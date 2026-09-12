-- NEPSE institutional accumulation analytics schema
-- Optimized for millions of floorsheet rows.

CREATE TABLE IF NOT EXISTS floorsheet (
    trade_date     DATE           NOT NULL,
    contract_id    BIGINT         NOT NULL,
    symbol         VARCHAR(20)    NOT NULL,
    buyer_broker   SMALLINT       NOT NULL,
    seller_broker  SMALLINT       NOT NULL,
    quantity       INT            NOT NULL,
    rate           NUMERIC(10, 2) NOT NULL,
    amount         NUMERIC(14, 2) NOT NULL,
    PRIMARY KEY (trade_date, contract_id)
);

CREATE INDEX IF NOT EXISTS idx_floorsheet_symbol_date
    ON floorsheet (symbol, trade_date);
CREATE INDEX IF NOT EXISTS idx_floorsheet_buyer_date
    ON floorsheet (buyer_broker, trade_date);
CREATE INDEX IF NOT EXISTS idx_floorsheet_seller_date
    ON floorsheet (seller_broker, trade_date);

CREATE TABLE IF NOT EXISTS daily_broker_rollup (
    trade_date      DATE           NOT NULL,
    symbol          VARCHAR(20)    NOT NULL,
    broker_id       SMALLINT       NOT NULL,
    buy_qty         BIGINT         NOT NULL DEFAULT 0,
    buy_amount      NUMERIC(18, 2) NOT NULL DEFAULT 0,
    sell_qty        BIGINT         NOT NULL DEFAULT 0,
    sell_amount     NUMERIC(18, 2) NOT NULL DEFAULT 0,
    self_trade_qty  BIGINT         NOT NULL DEFAULT 0,
    matched_qty     BIGINT         NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_date, symbol, broker_id)
);

CREATE INDEX IF NOT EXISTS idx_rollup_symbol_date
    ON daily_broker_rollup (symbol, trade_date);
CREATE INDEX IF NOT EXISTS idx_rollup_broker_date
    ON daily_broker_rollup (broker_id, trade_date);

CREATE TABLE IF NOT EXISTS daily_market_summary (
    trade_date        DATE           NOT NULL,
    symbol            VARCHAR(20)    NOT NULL,
    close_price       NUMERIC(10, 2) NOT NULL,
    price_change_pct  NUMERIC(10, 4) NOT NULL DEFAULT 0,
    total_qty         BIGINT         NOT NULL,
    total_turnover    NUMERIC(18, 2) NOT NULL,
    turnover_rank     INT            NOT NULL,
    PRIMARY KEY (trade_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_summary_date_rank
    ON daily_market_summary (trade_date, turnover_rank);

-- Historical screening signals for streak detection / backtesting.
CREATE TABLE IF NOT EXISTS screener_signals_history (
    trade_date     DATE           NOT NULL,
    symbol         VARCHAR(20)    NOT NULL,
    turnover_rank  INT            NOT NULL,
    broker_id      SMALLINT       NOT NULL,
    net_1d         INT,
    net_5d         INT,
    net_22d        INT,
    net_66d        INT,
    margin_pct     NUMERIC(6, 2),
    t1_change_pct  NUMERIC(6, 2),
    signal         VARCHAR(30)    NOT NULL,
    track          VARCHAR(10)    NOT NULL, -- 'TRACK_A' or 'TRACK_B'
    created_at     TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date, symbol, broker_id, track)
);

CREATE INDEX IF NOT EXISTS idx_sig_history_sym ON screener_signals_history(symbol, trade_date);
CREATE INDEX IF NOT EXISTS idx_sig_history_broker ON screener_signals_history(broker_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_sig_history_signal ON screener_signals_history(signal, trade_date);
