"""Typed configuration: YAML file + environment overlay, with the safety locks.

Secrets are read from the environment ONLY. Nothing here ever reads a private key
out of the YAML file, so a committed config can never leak one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

MAINNET_API = "https://api.hyperliquid.xyz"
TESTNET_API = "https://api.hyperliquid-testnet.xyz"
MAINNET_WS = "wss://api.hyperliquid.xyz/ws"
TESTNET_WS = "wss://api.hyperliquid-testnet.xyz/ws"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

ENV_PRIVATE_KEY = "HYPERLIQUID_PRIVATE_KEY"
ENV_ACCOUNT_ADDRESS = "HYPERLIQUID_ACCOUNT_ADDRESS"


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #


@dataclass
class DiscoveryConfig:
    """Filters applied to the ~44k-account leaderboard before deep scoring."""

    # Stage 1 — cheap filters on leaderboard rows (no extra HTTP per account).
    min_account_value: float = 10_000.0
    max_account_value: float = 500_000_000.0
    min_all_time_pnl: float = 5_000.0
    min_volume: float = 250_000.0
    # Stage 2 — how many survivors get a full equity-curve fetch. Each costs one
    # request, so this is the main knob on scan time.
    deep_scan_limit: int = 300
    # Share of the deep-scan budget reserved for the climber funnel.
    rising_scan_share: float = 0.40
    concurrency: int = 8

    # Quality floors applied after scoring (both rosters).
    min_days_active: int = 21
    max_drawdown: float = 0.35
    min_consistency: float = 0.45
    min_r_squared: float = 0.30
    max_concentration: float = 0.70
    max_turnover: float = 400.0

    # The "on the come up" climber band.
    rising_min_equity: float = 10_000.0
    rising_max_equity: float = 2_000_000.0
    rising_max_drawdown: float = 0.25
    rising_min_consistency: float = 0.50
    rising_min_r_squared: float = 0.40
    rising_min_pace_ratio: float = 0.25
    rising_min_days_active: int = 21

    refresh_minutes: int = 360
    # How often to re-study accounts and recompute the positioning consensus.
    research_refresh_minutes: int = 20


@dataclass
class CopyConfig:
    """How leader books are translated into ours."""

    enabled: bool = True
    # Which roster feeds the leader set: elite | rising | blend
    roster: str = "blend"
    max_leaders: int = 5
    rising_slots: int = 2  # of max_leaders, how many are reserved for climbers
    # equal | score  — score weighting allocates proportionally to composite score
    allocation: str = "score"
    manual_leaders: list[str] = field(default_factory=list)

    exposure_multiplier: float = 0.25
    deadband_pct: float = 0.02
    min_order_usd: float = 12.0
    reconcile_seconds: int = 45
    fill_trigger: bool = True  # WS leader fills trigger an early reconcile
    close_orphans: bool = True  # flatten coins no leader holds any more


@dataclass
class RiskConfig:
    max_gross_exposure: float = 3.0
    max_position_pct: float = 0.50
    max_leverage: float = 5.0
    max_concurrent_positions: int = 8
    daily_loss_limit: float = 0.10
    max_drawdown_limit: float = 0.25
    min_account_value: float = 50.0
    slippage_bps: float = 30.0
    coin_allowlist: list[str] = field(default_factory=list)
    coin_denylist: list[str] = field(default_factory=list)
    flatten_on_kill: bool = False
    use_isolated_margin: bool = False


@dataclass
class NotifyConfig:
    console: bool = True
    telegram_enabled: bool = False
    discord_enabled: bool = False
    webhook_enabled: bool = False
    webhook_url: str = ""
    min_severity: str = "INFO"
    cooldown_seconds: int = 60
    daily_summary_hour_utc: int = 0
    # Secrets come from env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DISCORD_WEBHOOK_URL
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)
    discord_webhook: str = field(default="", repr=False)


@dataclass
class Config:
    network: str = "testnet"
    dry_run: bool = True
    i_understand_live_trading_risk: bool = False
    account_address: str = ""
    log_level: str = "INFO"
    log_file: str = "hyperbot.log"
    db_path: str = "hyperbot.db"

    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    copy: CopyConfig = field(default_factory=CopyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    # ---- derived endpoints ------------------------------------------------ #
    @property
    def is_mainnet(self) -> bool:
        return self.network.lower() == "mainnet"

    @property
    def trade_api_url(self) -> str:
        return MAINNET_API if self.is_mainnet else TESTNET_API

    @property
    def public_api_url(self) -> str:
        """Public reads ALWAYS come from mainnet — testnet has no traders worth copying."""
        return MAINNET_API

    @property
    def ws_url(self) -> str:
        return MAINNET_WS

    @property
    def private_key(self) -> str:
        return os.getenv(ENV_PRIVATE_KEY, "").strip()

    # ---- the three live-trading locks ------------------------------------- #
    @property
    def can_trade_live(self) -> bool:
        return (
            not self.dry_run
            and self.i_understand_live_trading_risk
            and bool(self.private_key)
        )

    def live_blockers(self) -> list[str]:
        """Human-readable reasons live trading is disabled. Empty means armed."""
        blockers = []
        if self.dry_run:
            blockers.append("dry_run is true")
        if not self.i_understand_live_trading_risk:
            blockers.append("i_understand_live_trading_risk is false")
        if not self.private_key:
            blockers.append(f"{ENV_PRIVATE_KEY} is not set")
        return blockers


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #


def _load_env_file(path: str = ".env") -> None:
    """Minimal .env reader so the bot works without python-dotenv."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    except FileNotFoundError:
        pass


def _apply(target: Any, data: dict[str, Any], path: str = "") -> list[str]:
    """Recursively copy known keys onto a dataclass. Returns unknown key paths."""
    unknown: list[str] = []
    valid = {f.name: f for f in fields(target)}
    for key, value in (data or {}).items():
        where = f"{path}{key}"
        spec = valid.get(key)
        if spec is None:
            unknown.append(where)
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            unknown.extend(_apply(current, value, f"{where}."))
        elif isinstance(current, bool):
            setattr(target, key, bool(value))
        elif isinstance(current, float) and isinstance(value, (int, float)):
            setattr(target, key, float(value))
        else:
            setattr(target, key, value)
    return unknown


def load_config(config_path: str | None = None) -> tuple[Config, list[str]]:
    """Load YAML (if present) then overlay environment. Returns (config, warnings)."""
    _load_env_file()
    config = Config()
    warnings: list[str] = []

    path = Path(config_path or "config.yaml")
    if path.exists():
        try:
            import yaml  # imported lazily so `--help` works without PyYAML
        except ImportError:
            warnings.append("PyYAML not installed - config.yaml ignored, using defaults")
        else:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            unknown = _apply(config, raw)
            warnings += [f"unknown config key ignored: {key}" for key in unknown]
    elif config_path:
        warnings.append(f"config file not found: {path} - using defaults")

    # Environment overlay. Secrets are env-only by design.
    config.account_address = (
        os.getenv(ENV_ACCOUNT_ADDRESS, "").strip() or config.account_address
    )
    config.notify.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    config.notify.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    config.notify.discord_webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    if os.getenv("DRY_RUN") is not None:
        config.dry_run = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes", "on")
    if os.getenv("NETWORK"):
        config.network = os.getenv("NETWORK", "testnet").strip().lower()

    # Auto-enable channels whose credentials are actually present.
    if config.notify.telegram_token and config.notify.telegram_chat_id:
        config.notify.telegram_enabled = True
    if config.notify.discord_webhook:
        config.notify.discord_enabled = True
    if config.notify.webhook_url:
        config.notify.webhook_enabled = True

    # If the operator gave a key but no address, derive the address from the key.
    if not config.account_address and config.private_key:
        try:
            from eth_account import Account

            config.account_address = Account.from_key(config.private_key).address
        except Exception:  # noqa: BLE001 - address stays empty; caller reports it
            warnings.append("could not derive account address from private key")

    warnings.extend(_validate(config))
    return config, warnings


def _validate(config: Config) -> list[str]:
    warnings: list[str] = []
    if config.network.lower() not in ("mainnet", "testnet"):
        warnings.append(f"unknown network '{config.network}' - treating as testnet")
    if config.copy.roster not in ("elite", "rising", "blend"):
        warnings.append(f"unknown roster '{config.copy.roster}' - using 'blend'")
        config.copy.roster = "blend"
    if config.copy.allocation not in ("equal", "score"):
        warnings.append(f"unknown allocation '{config.copy.allocation}' - using 'score'")
        config.copy.allocation = "score"
    if config.copy.exposure_multiplier > 1.0:
        warnings.append(
            f"exposure_multiplier {config.copy.exposure_multiplier} > 1.0 - you will "
            "take MORE risk than the traders you copy"
        )
    if config.risk.max_leverage > 10:
        warnings.append(f"max_leverage {config.risk.max_leverage} is very high")
    if config.copy.rising_slots > config.copy.max_leaders:
        warnings.append("rising_slots exceeds max_leaders - clamping")
        config.copy.rising_slots = config.copy.max_leaders
    return warnings
