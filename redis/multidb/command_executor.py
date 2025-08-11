from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import List, Union, Optional

from redis.event import EventDispatcherInterface, OnCommandFailEvent
from redis.multidb.config import DEFAULT_AUTO_FALLBACK_INTERVAL
from redis.multidb.database import Database, AbstractDatabase, Databases
from redis.multidb.circuit import State as CBState
from redis.multidb.event import RegisterCommandFailure
from redis.multidb.failover import FailoverStrategy
from redis.multidb.failure_detector import FailureDetector
from redis.retry import Retry


class CommandExecutor(ABC):

    @property
    @abstractmethod
    def failure_detectors(self) -> List[FailureDetector]:
        """Returns a list of failure detectors."""
        pass

    @abstractmethod
    def add_failure_detector(self, failure_detector: FailureDetector) -> None:
        """Adds new failure detector to the list of failure detectors."""
        pass

    @property
    @abstractmethod
    def databases(self) -> Databases:
        """Returns a list of databases."""
        pass

    @property
    @abstractmethod
    def active_database(self) -> Union[Database, None]:
        """Returns currently active database."""
        pass

    @active_database.setter
    @abstractmethod
    def active_database(self, database: AbstractDatabase) -> None:
        """Sets currently active database."""
        pass

    @property
    @abstractmethod
    def failover_strategy(self) -> FailoverStrategy:
        """Returns failover strategy."""
        pass

    @property
    @abstractmethod
    def auto_fallback_interval(self) -> float:
        """Returns auto-fallback interval."""
        pass

    @auto_fallback_interval.setter
    @abstractmethod
    def auto_fallback_interval(self, auto_fallback_interval: float) -> None:
        """Sets auto-fallback interval."""
        pass

    @property
    @abstractmethod
    def command_retry(self) -> Retry:
        """Returns command retry object."""
        pass

    @abstractmethod
    def execute_command(self, *args, **options):
        """Executes a command and returns the result."""
        pass


class DefaultCommandExecutor(CommandExecutor):

    def __init__(
            self,
            failure_detectors: List[FailureDetector],
            databases: Databases,
            command_retry: Retry,
            failover_strategy: FailoverStrategy,
            event_dispatcher: EventDispatcherInterface,
            auto_fallback_interval: float = DEFAULT_AUTO_FALLBACK_INTERVAL,
    ):
        """
        :param failure_detectors: List of failure detectors.
        :param databases: List of databases.
        :param failover_strategy: Strategy that defines the failover logic.
        :param event_dispatcher: Event dispatcher.
        :param auto_fallback_interval: Interval between fallback attempts. Fallback to a new database according to
        failover_strategy.
        """
        self._failure_detectors = failure_detectors
        self._databases = databases
        self._command_retry = command_retry
        self._failover_strategy = failover_strategy
        self._event_dispatcher = event_dispatcher
        self._auto_fallback_interval = auto_fallback_interval
        self._next_fallback_attempt: datetime
        self._active_database: Union[Database, None] = None
        self._setup_event_dispatcher()
        self._schedule_next_fallback()

    @property
    def failure_detectors(self) -> List[FailureDetector]:
        return self._failure_detectors

    def add_failure_detector(self, failure_detector: FailureDetector) -> None:
        self._failure_detectors.append(failure_detector)

    @property
    def databases(self) -> Databases:
        return self._databases

    @property
    def command_retry(self) -> Retry:
        return self._command_retry

    @property
    def active_database(self) -> Optional[AbstractDatabase]:
        return self._active_database

    @active_database.setter
    def active_database(self, database: AbstractDatabase) -> None:
        self._active_database = database

    @property
    def failover_strategy(self) -> FailoverStrategy:
        return self._failover_strategy

    @property
    def auto_fallback_interval(self) -> float:
        return self._auto_fallback_interval

    @auto_fallback_interval.setter
    def auto_fallback_interval(self, auto_fallback_interval: int) -> None:
        self._auto_fallback_interval = auto_fallback_interval

    def execute_command(self, *args, **options):
        self._check_active_database()

        return self._command_retry.call_with_retry(
            lambda: self._execute_command(*args, **options),
            lambda error: self._on_command_fail(error, *args),
        )

    def _execute_command(self, *args, **options):
        self._check_active_database()
        return self._active_database.client.execute_command(*args, **options)

    def _on_command_fail(self, error, *args):
        self._event_dispatcher.dispatch(OnCommandFailEvent(args, error))

    def _check_active_database(self):
        """
        Checks if active a database needs to be updated.
        """
        if (
                self._active_database is None
                or self._active_database.circuit.state != CBState.CLOSED
                or (
                    self._auto_fallback_interval != DEFAULT_AUTO_FALLBACK_INTERVAL
                    and self._next_fallback_attempt <= datetime.now()
                )
        ):
            self._active_database = self._failover_strategy.database
            self._schedule_next_fallback()

    def _schedule_next_fallback(self) -> None:
        if self._auto_fallback_interval == DEFAULT_AUTO_FALLBACK_INTERVAL:
            return

        self._next_fallback_attempt = datetime.now() + timedelta(seconds=self._auto_fallback_interval)

    def _setup_event_dispatcher(self):
        """
        Registers command failure event listener.
        """
        event_listener = RegisterCommandFailure(self._failure_detectors)
        self._event_dispatcher.register_listeners({
            OnCommandFailEvent: [event_listener],
        })