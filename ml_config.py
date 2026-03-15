from pathlib import Path
from dataclasses import dataclass
import os


# Base project paths
PROJECT_ROOT = Path(r"D:\PROJECTS\github\POLYTRADE")
DATA_DIR = PROJECT_ROOT / "AI_dataset"
FEATURES_PATH = PROJECT_ROOT / "btc_5min_features_labels.parquet"
MODEL_PATH = PROJECT_ROOT / "btc_5min_xgb.json"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class DataConfig:
    """
    Configuration for offline data loading and feature generation.
    """

    data_dir: Path = DATA_DIR
    features_path: Path = FEATURES_PATH
    symbol_prefix: str = "BTCUSDT-1m"


@dataclass
class TrainConfig:
    """
    Training + backtest hyperparameters for the BTC 5m model.
    Tuned for short-horizon, noisy crypto tabular data.
    """

    features_path: Path = FEATURES_PATH
    model_path: Path = MODEL_PATH
    train_fraction: float = 0.8  # first 80% train, last 20% test
    random_seed: int = 42

    # XGBoost hyperparameters
    n_estimators: int = 400
    max_depth: int = 6
    learning_rate: float = 0.06
    subsample: float = 0.9
    colsample_bytree: float = 0.8

    # Backtest params
    prob_threshold: float = 0.68
    edge_buffer: float = 0.10  # assume entry price ~ prob - edge_buffer/2
    notional_per_trade: float = 100.0


@dataclass
class LiveConfig:
    """
    Live / paper-trading configuration for the Polymarket 5-minute BTC market.
    """

    # Binance streaming
    binance_api_key: str = os.environ.get("BINANCE_API_KEY", "")
    binance_api_secret: str = os.environ.get("BINANCE_API_SECRET", "")
    binance_symbol: str = "BTCUSDT"
    binance_interval: str = "1m"

    # 5-minute window structure (aligned to :00, :05, :10, ...)
    window_minutes: int = 5
    polling_interval_sec: int = 30
    min_candles_in_window: int = 4

    # Polymarket BTC 5-minute Up/Down market query
    polymarket_gamma_url: str = (
        "https://gamma-api.polymarket.com/markets"
        "?active=true&question_contains=Bitcoin+Up+or+Down+5+Minutes"
    )
    polymarket_clob_url: str = "https://clob.polymarket.com/orderbooks"

    # Model thresholds
    model_path: Path = MODEL_PATH
    prob_threshold: float = 0.68
    edge_buffer: float = 0.09  # require prob_up - yes_price >= edge_buffer

    # Risk management
    starting_balance: float = 10_000.0
    max_risk_fraction: float = 0.03      # at most 3% of balance per trade
    daily_loss_limit_fraction: float = 0.08  # stop for the day at -8%

