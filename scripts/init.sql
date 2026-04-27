-- Lords Bot Database Initialization
-- Run this script when setting up PostgreSQL for the first time

-- Create database (if not exists)
-- Note: Database creation is handled by docker-compose environment variables

-- Create tables
CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    trade_id VARCHAR(50) UNIQUE NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    side VARCHAR(10) NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price DECIMAL(10,2) NOT NULL,
    exit_price DECIMAL(10,2),
    entry_time TIMESTAMP NOT NULL,
    exit_time TIMESTAMP,
    pnl DECIMAL(10,2) DEFAULT 0,
    status VARCHAR(20) DEFAULT 'OPEN',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS state_snapshots (
    id SERIAL PRIMARY KEY,
    snapshot_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    state_data JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    event_type VARCHAR(50) NOT NULL,
    event_data JSONB,
    user_id VARCHAR(50),
    ip_address INET
);

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
CREATE INDEX IF NOT EXISTS idx_state_snapshots_time ON state_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_audit_log_time ON audit_log(event_time);

-- Create updated_at trigger function
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Add triggers
CREATE TRIGGER update_trades_updated_at
    BEFORE UPDATE ON trades
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();