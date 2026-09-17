"""Predictor for live AI Confidence scoring."""

import os
import xgboost as xgb
import pandas as pd

_MODEL = None
_MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'models', 'xgboost_momentum.json')

def _load_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
        
    if not os.path.exists(_MODEL_PATH):
        return None
        
    try:
        model = xgb.XGBClassifier()
        model.load_model(_MODEL_PATH)
        _MODEL = model
        return _MODEL
    except Exception as e:
        print(f"Error loading XGBoost model: {e}")
        return None

def predict_breakout_prob(turnover_ratio: float, rank_drift: float) -> float | None:
    """
    Predicts the probability of a breakout (>3% yield in 5 days) given live momentum features.
    Returns a float between 0.0 and 1.0, or None if the model cannot be loaded.
    """
    model = _load_model()
    if model is None:
        return None
        
    if turnover_ratio is None or rank_drift is None:
        return None
        
    # XGBoost requires a DataFrame or 2D array matching training features
    X = pd.DataFrame([{
        'turnover_ratio': turnover_ratio,
        'rank_drift': rank_drift
    }])
    
    try:
        prob = model.predict_proba(X)[0][1] # Probability of class 1
        return float(prob)
    except Exception:
        return None
