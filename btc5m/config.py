from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DataConfig:
    data_dir: Path = Path(r"D:\PROJECTS\github\POLYTRADE\AI_dataset")
    file_patterns: tuple[str, ...] = (
        "BTCUSDT-1m-2025-*.csv",
        "BTCUSDT-1m-2026-*.csv",
    )
    dataset_out_parquet: Path = Path("artifacts/btc_5m_features.parquet")
    dataset_out_csv: Path = Path("artifacts/btc_5m_features.csv")


@dataclass
class ModelConfig:
    test_size: float = 0.25
    random_state: int = 42
    n_search_iterations: int = 16
    model_out_json: Path = Path("artifacts/btc_5min_momentum_xgb.json")
    report_out_json: Path = Path("artifacts/train_report.json")
    predictions_out_csv: Path = Path("artifacts/test_predictions.csv")


@dataclass
class SimConfig:
    buy_probability_threshold: float = 0.68
    min_edge_threshold: float = 0.08
    default_yes_price: float = 0.50


@dataclass
class LiveConfig:
    symbol: str = "BTCUSDT"
    poll_seconds: int = 30
    gamma_markets_url: str = "https://gamma-api.polymarket.com/markets"
    polymarket_event_slug: str = "btc-updown-5m"
    starting_bankroll: float = 10_000.0
    fixed_fraction_per_trade: float = 0.02
    log_file: Path = Path("artifacts/live_paper.log")


@dataclass
class AppConfig:
    data: DataConfig = field(default_factory=DataConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    sim: SimConfig = field(default_factory=SimConfig)
    live: LiveConfig = field(default_factory=LiveConfig)

    def ensure_dirs(self) -> None:
        artifact_dir = self.model.model_out_json.parent
        artifact_dir.mkdir(parents=True, exist_ok=True)


def build_default_config() -> AppConfig:
    cfg = AppConfig()
    cfg.ensure_dirs()
    return cfg
