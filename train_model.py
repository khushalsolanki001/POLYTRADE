from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    precision_recall_fscore_support,
    roc_auc_score,
)
from xgboost import XGBClassifier

from ml_config import DataConfig, TrainConfig, LOG_DIR


def _setup_logger():
    import logging

    logger = logging.getLogger("train_model")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.FileHandler(LOG_DIR / "train_model.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.addHandler(fh)
    return logger


logger = _setup_logger()


def _load_dataset(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() in (".parquet", ".pq"):
        return pd.read_parquet(path)
    return pd.read_csv(path, index_col=0, parse_dates=True)


def _time_split(df: pd.DataFrame, train_fraction: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    split_idx = int(len(df) * train_fraction)
    return df.iloc[:split_idx].copy(), df.iloc[split_idx:].copy()


def _train_xgb(X_train: np.ndarray, y_train: np.ndarray, cfg: TrainConfig) -> XGBClassifier:
    model = XGBClassifier(
        n_estimators=cfg.n_estimators,
        max_depth=cfg.max_depth,
        learning_rate=cfg.learning_rate,
        subsample=cfg.subsample,
        colsample_bytree=cfg.colsample_bytree,
        objective="binary:logistic",
        eval_metric="logloss",
        tree_method="hist",
        n_jobs=-1,
        random_state=cfg.random_seed,
    )
    model.fit(X_train, y_train)
    return model


def _evaluate(model: XGBClassifier, X_test: np.ndarray, y_test: np.ndarray) -> Dict[str, float]:
    proba = model.predict_proba(X_test)[:, 1]
    preds = (proba >= 0.5).astype(int)

    acc = accuracy_score(y_test, preds)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_test, preds, average="binary", pos_label=1
    )
    roc = roc_auc_score(y_test, proba)
    cm = confusion_matrix(y_test, preds)

    logger.info("Test accuracy: %.4f", acc)
    logger.info("Precision (Up=1): %.4f", precision)
    logger.info("Recall (Up=1): %.4f", recall)
    logger.info("F1 (Up=1): %.4f", f1)
    logger.info("ROC-AUC: %.4f", roc)
    logger.info("Confusion matrix:\n%s", cm)

    return {
        "accuracy": acc,
        "precision_up": precision,
        "recall_up": recall,
        "f1_up": f1,
        "roc_auc": roc,
    }


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return float(dd.min())


def _backtest(df_test: pd.DataFrame, proba: np.ndarray, cfg: TrainConfig) -> Dict[str, float]:
    """
    Simple offline backtest on test set.

    Entry rule:
      - enter at end of minute 4 when prob_up > prob_threshold
      - approximate entry_implied_prob ~ prob_up - edge_buffer/2

    PnL per contract:
      - correct: +(1 - entry_price)
      - wrong  : -entry_price
    """
    df = df_test.copy()
    df["prob_up"] = proba
    df["entry_prob"] = (df["prob_up"] - cfg.edge_buffer / 2).clip(0.01, 0.99)

    trades = df[df["prob_up"] > cfg.prob_threshold].copy()
    if trades.empty:
        logger.warning("No trades triggered with current thresholds.")
        return {
            "num_trades": 0,
            "win_rate": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown": 0.0,
        }

    correct = trades["label"] == 1
    entry = trades["entry_prob"].values
    pnl_per_contract = np.where(correct.to_numpy(), 1.0 - entry, -entry)
    pnl_dollar = pnl_per_contract * cfg.notional_per_trade

    equity = pd.Series(pnl_dollar).cumsum()
    total_return = pnl_dollar.sum() / (cfg.notional_per_trade * len(trades))
    win_rate = float(correct.mean())
    mdd = _max_drawdown(equity)

    logger.info("Backtest trades: %d", len(trades))
    logger.info("Win rate: %.4f", win_rate)
    logger.info("Total return (per-trade notional basis): %.4f", total_return)
    logger.info("Max drawdown: %.4f", mdd)

    return {
        "num_trades": int(len(trades)),
        "win_rate": win_rate,
        "total_return_pct": float(total_return),
        "max_drawdown": mdd,
    }


def main():
    data_cfg = DataConfig()
    train_cfg = TrainConfig()

    logger.info("Loading dataset from %s", data_cfg.features_path)
    df = _load_dataset(data_cfg.features_path)

    # Features = all columns except label/start_price/end_price
    feature_cols = [c for c in df.columns if c not in ("label", "start_price", "end_price")]
    train_df, test_df = _time_split(df, train_cfg.train_fraction)

    X_train = train_df[feature_cols].values
    y_train = train_df["label"].astype(int).values
    X_test = test_df[feature_cols].values
    y_test = test_df["label"].astype(int).values

    logger.info("Train samples: %d, Test samples: %d", len(train_df), len(test_df))

    logger.info("Training XGBoost model…")
    model = _train_xgb(X_train, y_train, train_cfg)

    logger.info("Evaluating on test set…")
    metrics = _evaluate(model, X_test, y_test)

    logger.info("Running backtest on test set…")
    proba_test = model.predict_proba(X_test)[:, 1]
    bt = _backtest(test_df, proba_test, train_cfg)

    out_path = Path(train_cfg.model_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_path))
    logger.info("Saved model to %s", out_path)

    logger.info("Metrics: %s", metrics)
    logger.info("Backtest: %s", bt)


if __name__ == "__main__":
    main()

