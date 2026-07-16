"""画面ごとのBehaviorを統一的に扱うController。"""

from __future__ import annotations

from typing import Generic, Protocol, TypeVar, assert_never, cast

from agent_workflow.tui.state import (
    ArtifactState,
    AttemptsState,
    DashboardState,
    LogsState,
    RunDetailState,
    ScreenState,
    TuiContext,
    dashboard_state_for,
    detail_state_for,
)

from .artifact import ArtifactBehavior
from .attempts import AttemptsBehavior
from .dashboard import DashboardBehavior
from .detail import RunDetailBehavior
from .logs import LogsBehavior
from .result import BehaviorResult


StateT = TypeVar("StateT")


class BehaviorProtocol(Protocol[StateT]):
    """画面Controllerが必要とするBehaviorの共通契約。"""

    def handle(self, state: StateT, key: int) -> BehaviorResult:
        ...

    def refresh(self, state: StateT) -> StateT:
        ...


class ScreenController(Protocol):
    """TuiAppから見た画面Controllerの共通インターフェース。"""

    view: str

    def handle(self, state: ScreenState, key: int) -> BehaviorResult:
        ...

    def refresh(self, state: ScreenState) -> ScreenState:
        ...


class TypedScreenController(Generic[StateT]):
    """State型付きBehaviorをScreenControllerへ適合させる。"""

    def __init__(self, view: str, state_type: type[StateT], behavior: BehaviorProtocol[StateT]) -> None:
        self.view = view
        self.state_type = state_type
        self.behavior = behavior

    def handle(self, state: ScreenState, key: int) -> BehaviorResult:
        return self.behavior.handle(self._typed_state(state), key)

    def refresh(self, state: ScreenState) -> ScreenState:
        return self.behavior.refresh(self._typed_state(state))

    def _typed_state(self, state: ScreenState) -> StateT:
        if not isinstance(state, self.state_type):
            raise TypeError(
                f"{self.view}画面には{self.state_type.__name__}が必要です: {type(state).__name__}"
            )
        return cast(StateT, state)


class ScreenControllers:
    """全画面Controllerの登録とState遷移を担当する。"""

    def __init__(self, context: TuiContext) -> None:
        self.dashboard = TypedScreenController("dashboard", DashboardState, DashboardBehavior(context))
        self.detail = TypedScreenController("detail", RunDetailState, RunDetailBehavior(context))
        self.attempts = TypedScreenController("attempts", AttemptsState, AttemptsBehavior(context))
        self.logs = TypedScreenController("logs", LogsState, LogsBehavior(context))
        self.artifact = TypedScreenController("artifact", ArtifactState, ArtifactBehavior(context))

    def resolve(self, state: ScreenState) -> ScreenController:
        match state:
            case DashboardState():
                return self.dashboard
            case RunDetailState():
                return self.detail
            case AttemptsState():
                return self.attempts
            case LogsState():
                return self.logs
            case ArtifactState():
                return self.artifact
            case _ as unreachable:
                assert_never(unreachable)

    def state_for_view(
        self,
        state: ScreenState,
        view: str,
        *,
        artifact_kind: str,
    ) -> ScreenState | None:
        """互換用view setterの遷移をController側で解決する。"""

        match view:
            case "dashboard":
                return dashboard_state_for(state)
            case "detail":
                detail = detail_state_for(state)
                return detail if detail.detail is not None else state
            case "attempts":
                detail = detail_state_for(state)
                return self.attempts.behavior.open(detail) if detail.detail is not None else state
            case "logs":
                detail = detail_state_for(state)
                return self.logs.behavior.open(detail) if detail.detail is not None else state
            case "artifact":
                detail = detail_state_for(state)
                return self.artifact.behavior.open(detail, artifact_kind) if detail.detail is not None else state
            case _:
                return None

    def open_attempts(self, state: ScreenState) -> BehaviorResult:
        match state:
            case RunDetailState():
                return self.detail.handle(state, ord("a"))
            case LogsState():
                return self.logs.handle(state, ord("a"))
            case _:
                return BehaviorResult(state)

    def open_logs(self, state: ScreenState) -> BehaviorResult:
        match state:
            case RunDetailState() if state.detail is not None:
                return BehaviorResult(self.logs.behavior.open(state))
            case AttemptsState():
                return self.attempts.handle(state, ord("l"))
            case _:
                return BehaviorResult(state)

    def open_artifact(self, state: ScreenState, kind: str) -> ArtifactState | None:
        detail = detail_state_for(state)
        if detail.detail is None:
            return None
        return self.artifact.behavior.open(detail, kind)
