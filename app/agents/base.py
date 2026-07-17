from __future__ import annotations

from abc import ABC, abstractmethod


class AgentResult(dict):
    pass


class BaseAgent(ABC):
    @abstractmethod
    def run(self, payload: dict) -> AgentResult:
        raise NotImplementedError

