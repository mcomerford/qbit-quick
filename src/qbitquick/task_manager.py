import logging
import threading
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)


class TaskManager:
    """
    A thread-safe manager for running and tracking asynchronous tasks.

    Tasks are run in daemon threads and can be cancelled cooperatively using a stop_event.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start_task(self, target: Callable, *args: Any, task_name: str, **kwargs: Any) -> str:
        """
        Start a new asynchronous task using the provided function.

        The function receives a `stop_event` keyword argument for cooperative cancellation.

        Returns:
            str: A unique task ID.
        """
        task_id = str(uuid.uuid4())
        stop_event = threading.Event()

        def run() -> None:
            # noinspection PyBroadException
            try:
                kwargs["stop_event"] = stop_event
                target(*args, **kwargs)
            except TaskInterrupted as e:
                logger.info(e.message)
                pass
            except Exception:
                logger.exception("Unhandled exception")
            finally:
                self._remove_task(task_id)

        thread = threading.Thread(target=run, daemon=False)

        with self._lock:
            self._tasks[task_id] = {
                "name": task_name,
                "thread": thread,
                "stop_event": stop_event,
                "args": args,
                "kwargs": kwargs,
            }

        thread.start()
        return task_id

    def _remove_task(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def get_running_tasks(self) -> dict[str, str]:
        with self._lock:
            return {
                task_info["name"]: task_id
                for task_id, task_info in self._tasks.items()
                if task_info["thread"].is_alive()
            }

    def cancel_task(self, task_id: str) -> bool:
        """
        Request cancellation of a task.

        Returns:
            bool: True if the task was found and cancel signal sent.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task["stop_event"].set()
                return True
            return False

    def cancel_all_tasks(self) -> None:
        """
        Request cancellation of all running tasks.
        """
        with self._lock:
            for task in self._tasks.values():
                task["stop_event"].set()

    def join_all_threads(self, timeout: float = 1.0) -> None:
        """
        Wait for all running task threads to finish.

        Args:
            timeout (float): Max time to wait per thread.
        """
        with self._lock:
            for task in self._tasks.values():
                thread = task["thread"]
                if thread.is_alive():
                    thread.join(timeout=timeout)


class TaskInterrupted(Exception):
    """Raised to indicate cooperative shutdown."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
