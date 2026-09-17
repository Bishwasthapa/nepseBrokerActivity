"""Core ML Trainer to classify profitable breakout probabilities."""

from __future__ import annotations

import pandas as pd
import os
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score
import xgboost as xgb
from src.ml.features import build_dataset

def train_classifier(conn, short_w=5, base_w=15):
    """
    Train an XGBoost model on the historical data to predict T+5 breakouts.
    """
    df = build_dataset(conn, short_w, base_w)
    
    # Feature Selection
    features = ['turnover_ratio', 'rank_drift']
    
    print(f"Total rows before dropna: {len(df)}")
    train_df = df.dropna(subset=features + ['target_t5_up3'])
    print(f"Total rows after dropna: {len(train_df)}")
    
    X = train_df[features]
    y = train_df['target_t5_up3']
    
    if len(X) < 100:
        print("Not enough data to train.")
        return None
        
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    model = xgb.XGBClassifier(
        n_estimators=100,
        learning_rate=0.05,
        max_depth=4,
        eval_metric='logloss',
        use_label_encoder=False
    )
    
    print("Training XGBoost Classifier...")
    model.fit(X_train, y_train)
    
    preds = model.predict(X_test)
    acc = accuracy_score(y_test, preds)
    
    print("\n" + "="*50)
    print("MODEL PERFORMANCE (T+5 Yield > 3%)")
    print("="*50)
    print(f"Accuracy: {acc:.2f}")
    print("\nClassification Report:")
    print(classification_report(y_test, preds))
    
    # Feature Importances
    importances = model.feature_importances_
    feat_imp = pd.DataFrame({
        'Feature': features,
        'Importance': importances
    }).sort_values('Importance', ascending=False)
    
    print("\nFeature Importances:")
    print(feat_imp.to_string(index=False))
    
    # Save the model
    os.makedirs('models', exist_ok=True)
    model_path = 'models/xgboost_momentum.json'
    model.save_model(model_path)
    print(f"\nModel saved to {model_path}")
    
    return model

if __name__ == "__main__":
    from src.db import get_conn
    conn = get_conn()
    try:
        train_classifier(conn)
    finally:
        conn.close()
