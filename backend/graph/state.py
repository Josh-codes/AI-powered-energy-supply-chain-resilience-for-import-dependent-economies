"""Thread-safe in-memory holder for the crude-import knowledge graph.

The graph is a process-wide singleton, rebuilt from the database on startup and
after each pipeline cycle. Read it with ``get_graph()`` for inspection; always
take ``get_graph_copy()`` before mutating it in a simulation so the shared
instance is never altered.
"""
import threading
from datetime import datetime, timezone


class GraphState:
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._lock = threading.RLock()
        self._graph = None
        self._built_at = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def set_graph(self, graph, built_at=None):
        with self._lock:
            self._graph = graph
            self._built_at = built_at or datetime.now(timezone.utc)

    def get_graph(self):
        """Return the live graph. Do NOT mutate — use ``get_graph_copy()``."""
        with self._lock:
            return self._graph

    def get_graph_copy(self):
        """Return a deep-enough copy safe to mutate in a scenario simulation."""
        with self._lock:
            return None if self._graph is None else self._graph.copy()

    def is_loaded(self):
        with self._lock:
            return self._graph is not None

    @property
    def built_at(self):
        with self._lock:
            return self._built_at

    def clear(self):
        with self._lock:
            self._graph = None
            self._built_at = None
