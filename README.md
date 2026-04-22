
uvicorn backend.main:app --reload

python -m uvicorn backend.main:app --reload

http://127.0.0.1:8000/docs

python backtest_runner.py

python download_nifty_data.py

# Option-chain collector (stores CSV + JSONL under data/)
python -m backend.app.data.collect_option_chain
