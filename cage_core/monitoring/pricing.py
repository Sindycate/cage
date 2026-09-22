"""Validate and store private provider-qualified model rates."""

from __future__ import annotations

import math
from pathlib import Path

from . import constants as constants_api
from . import errors as errors_api
from . import state as state_api
from . import validation as validation_api


def _validate_model_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > 256
        or any(character in value for character in "\x00\r\n\t")
    ):
        raise errors_api.MonitorError("invalid pricing model id")
    return value.strip()


def _validate_pricing_key(value: object) -> str:
    key = _validate_model_id(value)
    if ":" not in key:
        return key
    provider, model = key.split(":", 1)
    if validation_api._provider_slug(provider) is None or not model or ":" in model:
        raise errors_api.MonitorError("invalid pricing provider/model key")
    return f"{validation_api._provider_slug(provider)}:{model}"


def _validate_unit_price(value: object, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise errors_api.MonitorError("pricing rates must be finite non-negative numbers")
    return float(value)


def load_pricing(config_root: Path) -> dict[str, dict[str, float]]:
    root = state_api.monitor_root(config_root)
    state_api._ensure_private_directory(root)
    value = state_api._read_json(root / constants_api.PRICING_FILE, max_bytes=constants_api.MAX_CONNECTION_BYTES)
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) != {"version", "models"} or value["version"] != constants_api.PRICING_VERSION:
        raise errors_api.MonitorError("monitor pricing has an invalid shape")
    models = value["models"]
    if not isinstance(models, dict) or len(models) > 1024:
        raise errors_api.MonitorError("monitor pricing models are invalid")
    result: dict[str, dict[str, float]] = {}
    allowed = {"input_per_million", "output_per_million", "cache_read_per_million", "cache_write_per_million"}
    for raw_model, raw_rates in models.items():
        model = _validate_pricing_key(raw_model)
        if not isinstance(raw_rates, dict) or set(raw_rates).difference(allowed):
            raise errors_api.MonitorError("monitor pricing entry has an invalid shape")
        if not ({"input_per_million", "output_per_million"} & set(raw_rates)):
            raise errors_api.MonitorError("monitor pricing needs an input or output rate")
        result[model] = {
            key: _validate_unit_price(rate)  # type: ignore[dict-item]
            for key, rate in raw_rates.items()
        }
    return result


def save_pricing(config_root: Path, models: dict[str, dict[str, float]]) -> None:
    normalized: dict[str, dict[str, float]] = {}
    if not isinstance(models, dict) or len(models) > 1024:
        raise errors_api.MonitorError("monitor pricing models are invalid")
    for model, rates in models.items():
        model_id = _validate_pricing_key(model)
        if not isinstance(rates, dict):
            raise errors_api.MonitorError("monitor pricing entry has an invalid shape")
        allowed = {"input_per_million", "output_per_million", "cache_read_per_million", "cache_write_per_million"}
        if set(rates).difference(allowed) or not ({"input_per_million", "output_per_million"} & set(rates)):
            raise errors_api.MonitorError("monitor pricing entry has an invalid shape")
        normalized[model_id] = {key: _validate_unit_price(value) for key, value in rates.items()}  # type: ignore[dict-item]
    state_api._write_json(
        state_api.monitor_root(config_root) / constants_api.PRICING_FILE,
        {"version": constants_api.PRICING_VERSION, "models": normalized},
    )


def set_model_pricing(
    config_root: Path,
    model: str,
    *,
    input_per_million: float | None,
    output_per_million: float | None,
    cache_read_per_million: float | None,
    cache_write_per_million: float | None = None,
) -> None:
    model_id = _validate_pricing_key(model)
    if input_per_million is None and output_per_million is None:
        raise errors_api.MonitorError("pricing needs --input or --output")
    rates = {
        key: value
        for key, value in {
            "input_per_million": _validate_unit_price(input_per_million, optional=True),
            "output_per_million": _validate_unit_price(output_per_million, optional=True),
            "cache_read_per_million": _validate_unit_price(cache_read_per_million, optional=True),
            "cache_write_per_million": _validate_unit_price(cache_write_per_million, optional=True),
        }.items()
        if value is not None
    }
    models = load_pricing(config_root)
    models[model_id] = rates  # type: ignore[assignment]
    save_pricing(config_root, models)


def remove_model_pricing(config_root: Path, model: str) -> bool:
    model_id = _validate_pricing_key(model)
    models = load_pricing(config_root)
    existed = model_id in models
    models.pop(model_id, None)
    save_pricing(config_root, models)
    return existed


def _write_tokscale_pricing(config_root: Path, state_path: Path) -> None:
    models = load_pricing(config_root)
    destination = state_path / "tokscale"
    state_api._ensure_private_directory(destination)
    upstream: dict[str, dict[str, float]] = {}
    mapping = {
        "input_per_million": "input_cost_per_million_tokens",
        "output_per_million": "output_cost_per_million_tokens",
        "cache_read_per_million": "cache_read_input_token_cost_per_million_tokens",
        "cache_write_per_million": "cache_creation_input_token_cost_per_million_tokens",
    }
    for model, rates in models.items():
        # Tokscale accepts model-only keys.  Provider-qualified prices are
        # applied by Cage after collection, so they must not be handed to the
        # upstream collector as if they were model IDs.
        if ":" in model:
            continue
        upstream[model] = {mapping[key]: value for key, value in rates.items()}
    state_api._write_json(destination / "custom-pricing.json", {"models": upstream})
