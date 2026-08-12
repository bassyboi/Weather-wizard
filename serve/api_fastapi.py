import os
from pathlib import Path
from typing import Annotated, Any

import xarray as xr
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ordcast.fusion import EvidenceWeightedScenarioEngine
from ordcast.types import SourceForecast

MAX_REQUEST_BYTES = 1_000_000
RainRow = Annotated[list[FiniteFloat], Field(min_length=1, max_length=64)]
RainMembers = Annotated[list[RainRow], Field(min_length=1, max_length=512)]
FlatWeights = Annotated[list[FiniteFloat], Field(min_length=1, max_length=512)]
MatrixWeights = Annotated[list[RainRow], Field(min_length=1, max_length=512)]

app = FastAPI(
    title="OrdCast RET",
    version="0.1.0",
    description="Regime–Event–Twin research scaffold for the Ord River district.",
)


class StrictRequestModel(BaseModel):
    """API input that never guesses the caller's intended types or fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class SourceScenarioRequest(StrictRequestModel):
    name: str = Field(min_length=1, max_length=64)
    members_mm: RainMembers
    member_weights: FlatWeights | MatrixWeights | None = None
    recent_crps: (
        Annotated[list[FiniteFloat], Field(min_length=1, max_length=64)]
        | FiniteFloat
        | None
    ) = None
    age_hours: FiniteFloat = Field(default=0.0, ge=0.0)
    half_life_hours: FiniteFloat = Field(default=24.0, gt=0.0)


class ScenarioForecastRequest(StrictRequestModel):
    horizons_hours: Annotated[list[StrictInt], Field(min_length=1, max_length=64)]
    sources: Annotated[list[SourceScenarioRequest], Field(min_length=1, max_length=16)]
    thresholds_mm: Annotated[list[FiniteFloat], Field(max_length=32)] = Field(
        default_factory=lambda: [1.0, 10.0, 25.0, 50.0],
        description="Return P(cumulative rainfall >= threshold) at each horizon.",
    )
    quantiles: Annotated[list[FiniteFloat], Field(min_length=1, max_length=19)] = Field(
        default_factory=lambda: [0.1, 0.5, 0.9]
    )


class _RequestBodyTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="request body too large")


class StreamingBodyLimitMiddleware:
    """Enforce a body cap as ASGI chunks arrive, without buffering a second copy."""

    _BODY_METHODS = {"POST", "PUT", "PATCH"}

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in self._BODY_METHODS:
            await self.app(scope, receive, send)
            return

        declared_lengths = [
            value
            for key, value in scope.get("headers", [])
            if key.lower() == b"content-length"
        ]
        if len(declared_lengths) > 1:
            await self._error(scope, receive, send, 400, "invalid content-length")
            return
        if declared_lengths:
            try:
                declared_length = int(declared_lengths[0].decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                await self._error(scope, receive, send, 400, "invalid content-length")
                return
            if declared_length < 0:
                await self._error(scope, receive, send, 400, "invalid content-length")
                return
            if declared_length > self.max_bytes:
                await self._error(scope, receive, send, 413, "request body too large")
                return

        received = 0
        response_started = False
        response_messages: list[Message] = []

        async def receive_with_limit() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def hold_response(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            response_messages.append(message)

        try:
            await self.app(scope, receive_with_limit, hold_response)
        except _RequestBodyTooLarge:
            await self._error(scope, receive, send, 413, "request body too large")
            return
        if response_started:
            for message in response_messages:
                await send(message)

    @staticmethod
    async def _error(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
    ) -> None:
        await JSONResponse(status_code=status_code, content={"detail": detail})(
            scope, receive, send
        )


app.add_middleware(StreamingBodyLimitMiddleware, max_bytes=MAX_REQUEST_BYTES)


@app.get("/")
def root():
    return {
        "message": "OrdCast RET marginal-fusion research scaffold running",
        "status": "research",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "engine": "evidence_weighted_marginals_v1",
        "forecast_dir": os.environ.get("FORECAST_DIR", "outputs"),
    }


@app.post("/v1/marginals")
def scenario_forecast(request: ScenarioForecastRequest) -> dict[str, Any]:
    """Fuse probabilistic rainfall marginals independently at each lead time."""
    try:
        if not request.sources:
            raise ValueError("at least one source forecast is required")
        sources = [
            SourceForecast(
                name=source.name,
                horizons_hours=request.horizons_hours,
                members_mm=source.members_mm,
                member_weights=source.member_weights,
                recent_crps=source.recent_crps,
                age_hours=source.age_hours,
                half_life_hours=source.half_life_hours,
            )
            for source in request.sources
        ]
        forecast = EvidenceWeightedScenarioEngine().fuse(sources)
        return forecast.to_dict(
            quantiles=request.quantiles,
            thresholds_mm=request.thresholds_mm,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/nowcast/meta")
def nowcast_meta(
    forecast_id: str = Query(
        "nowcast_60min.nc",
        description="Filename inside FORECAST_DIR; arbitrary filesystem paths are rejected.",
    )
):
    base = Path(os.environ.get("FORECAST_DIR", "outputs")).resolve()
    p = (base / forecast_id).resolve()
    if base != p.parent or p.suffix not in {".nc", ".zarr"}:
        raise HTTPException(status_code=400, detail="invalid forecast_id")
    if not p.exists():
        return {"exists": False, "forecast_id": forecast_id}
    opener = xr.open_zarr if p.suffix == ".zarr" else xr.open_dataset
    with opener(p) as dataset:
        return {
            "exists": True,
            "forecast_id": forecast_id,
            "variables": list(dataset.data_vars),
            "dimensions": {name: int(size) for name, size in dataset.sizes.items()},
            "coordinates": list(dataset.coords),
        }
