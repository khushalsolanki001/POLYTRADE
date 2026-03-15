from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import ParameterSampler
from xgboost import XGBClassifier

from .config import AppConfig, build_default_config
from .data import load_all_klines
from .features import FEATURE_COLUMNS, build_feature_dataset

LOGGER = logging.getLogger(__name__)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def save_feature_dataset(df: pd.DataFrame, cfg: AppConfig) -> None:
    cfg.ensure_dirs()
    try:
        df.to_parquet(cfg.data.dataset_out_parquet, index=False)
        LOGGER.info("Saved parquet dataset to %s", cfg.data.dataset_out_parquet)
    except Exception as exc:
        LOGGER.warning("Parquet save failed (%s). Falling back to CSV.", exc)
    df.to_csv(cfg.data.dataset_out_csv, index=False)
    LOGGER.info("Saved CSV dataset to %s", cfg.data.dataset_out_csv)


def chronological_split(df: pd.DataFrame, test_size: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    split_ix = int(len(df) * (1.0 - test_size))
    split_ix = max(1, min(split_ix, len(df) - 1))
    train_df = df.iloc[:split_ix].copy()
    test_df = df.iloc[split_ix:].copy()
    return train_df, test_df


def train_xgb_with_search(
    x_train: pd.DataFrame,
    y_train: pd.Series,
    x_valid: pd.DataFrame,
    y_valid: pd.Series,
    random_state: int,
    n_iter: int,
) -> XGBClassifier:
    param_grid = {
        "n_estimators": [200, 300, 400, 500],
        "max_depth": [4, 5, 6, 7, 8],
        "learning_rate": [0.03, 0.05, 0.08],
        "subsample": [0.8, 0.9, 1.0],
        "colsample_bytree": [0.7, 0.8, 0.9],
    }

    candidates = list(
        ParameterSampler(
            param_distributions=param_grid,
            n_iter=n_iter,
            random_state=random_state,
        )
    )

    best_model: XGBClassifier | None = None
    best_score = -np.inf

    for i, params in enumerate(candidates, start=1):
        model = XGBClassifier(
            objective="binary:logistic",
            eval_metric="logloss",
            tree_method="hist",
            random_state=random_state,
            n_jobs=-1,
            **params,
        )
        model.fit(x_train, y_train)
        prob = model.predict_proba(x_valid)[:, 1]
        auc = roc_auc_score(y_valid, prob)
        LOGGER.info("Model %d/%d params=%s auc=%.5f", i, len(candidates), params, auc)

        if auc > best_score:
            best_score = auc
            best_model = model

    if best_model is None:
        raise RuntimeError("Hyperparameter search did not produce a model.")
    return best_model


def compute_max_drawdown(cum_curve: np.ndarray) -> float:
    if cum_curve.size == 0:
        return 0.0
    running_peak = np.maximum.accumulate(cum_curve)
    drawdowns = cum_curve - running_peak
    return float(drawdowns.min())


def simulate_trading_performance(
    y_true: pd.Series,
    prob_up: np.ndarray,
    yes_price: np.ndarray,
    prob_threshold: float,
    edge_threshold: float,
) -> dict[str, float]:
    take = (prob_up >= prob_threshold) & (yes_price < (prob_up - edge_threshold))
    if take.sum() == 0:
        return {
            "num_trades": 0,
            "win_rate": 0.0,
            "avg_profit": 0.0,
            "cum_return": 0.0,
            "max_drawdown": 0.0,
        }

    y = y_true.to_numpy(dtype=int)
    pnl = np.where(y[take] == 1, 1.0 - yes_price[take], -yes_price[take])
    cum = np.cumsum(pnl)
    return {
        "num_trades": int(take.sum()),
        "win_rate": float((pnl > 0).mean()),
        "avg_profit": float(pnl.mean()),
        "cum_return": float(cum[-1]),
        "max_drawdown": float(compute_max_drawdown(cum)),
    }


def train_pipeline(cfg: AppConfig) -> dict[str, object]:
    raw = load_all_klines(cfg.data.data_dir, cfg.data.file_patterns)
    feat_df = build_feature_dataset(raw)
    save_feature_dataset(feat_df, cfg)

    train_df, test_df = chronological_split(feat_df, cfg.model.test_size)

    x_train = train_df[FEATURE_COLUMNS]
    y_train = train_df["label"]
    x_test = test_df[FEATURE_COLUMNS]
    y_test = test_df["label"]

    model = train_xgb_with_search(
        x_train=x_train,
        y_train=y_train,
        x_valid=x_test,
        y_valid=y_test,
        random_state=cfg.model.random_state,
        n_iter=cfg.model.n_search_iterations,
    )

    pred_cls = model.predict(x_test)
    pred_prob = model.predict_proba(x_test)[:, 1]

    metrics = {
        "accuracy": float(accuracy_score(y_test, pred_cls)),
        "precision_up": float(precision_score(y_test, pred_cls, pos_label=1, zero_division=0)),
        "recall_up": float(recall_score(y_test, pred_cls, pos_label=1, zero_division=0)),
        "roc_auc": float(roc_auc_score(y_test, pred_prob)),
        "log_loss": float(log_loss(y_test, pred_prob)),
        "confusion_matrix": confusion_matrix(y_test, pred_cls).tolist(),
    }

    yes_price = (
        test_df["yes_price_proxy"].to_numpy(dtype=float)
        if "yes_price_proxy" in test_df.columns
        else np.full(shape=len(test_df), fill_value=cfg.sim.default_yes_price)
    )
    sim = simulate_trading_performance(
        y_true=y_test,
        prob_up=pred_prob,
        yes_price=yes_price,
        prob_threshold=cfg.sim.buy_probability_threshold,
        edge_threshold=cfg.sim.min_edge_threshold,
    )

    cfg.ensure_dirs()
    model.save_model(cfg.model.model_out_json)
    pred_out = test_df[["window_start", "label", "yes_price_proxy"]].copy()
    pred_out["pred_prob_up"] = pred_prob
    pred_out["pred_label"] = pred_cls
    pred_out.to_csv(cfg.model.predictions_out_csv, index=False)

    report = {
        "metrics": metrics,
        "simulated_trading": sim,
        "rows_total": int(len(feat_df)),
        "rows_train": int(len(train_df)),
        "rows_test": int(len(test_df)),
        "feature_columns": FEATURE_COLUMNS,
        "model_path": str(cfg.model.model_out_json),
    }
    cfg.model.report_out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    LOGGER.info("Saved model to %s", cfg.model.model_out_json)
    LOGGER.info("Saved report to %s", cfg.model.report_out_json)
    LOGGER.info("Metrics: %s", json.dumps(metrics, indent=2))
    LOGGER.info("Simulated trading: %s", json.dumps(sim, indent=2))
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train BTC 5-minute momentum XGBoost model")
    parser.add_argument("--data-dir", type=str, default=None, help="Override AI dataset directory")
    parser.add_argument("--test-size", type=float, default=None, help="Chronological test split fraction")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging("INFO")

    cfg = build_default_config()
    if args.data_dir:
        cfg.data.data_dir = Path(args.data_dir)
    if args.test_size is not None:
        cfg.model.test_size = float(args.test_size)

    train_pipeline(cfg)


if __name__ == "__main__":
    main()
