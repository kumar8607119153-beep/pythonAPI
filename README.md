# SANJAY RANA — Delta Exchange Signal Dashboard

Signal-only dashboard. It uses public Delta Exchange market data; it does not require API keys and has no trading/order/position code.

## Included
- Requested timeframes: 1m, 5m, 15m, 1h
- Supertrend ATR 10, multiplier 2.0
- Live traded price via public WebSocket trades feed
- Signal price (latest direction flip in fetched candle history)
- TSL price (current Supertrend line)
- Exact symbol lookup: BTC/ETH candidates plus XAUTUSDT and OILUSD candidates
- No synthetic prices for instruments not listed by Delta

## Render
1. Upload `app.py`, `requirements.txt`, and `render.yaml` to a new GitHub repository.
2. In Render choose **New + → Blueprint** and select the repository, or create a Python Web Service.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --workers 1 --threads 8 --timeout 120`
5. Use a paid always-on instance if 24/7 availability is required. Render free web services can spin down.

## Important notes
- Delta product names vary by market/region. The dashboard checks Delta's public product list and only displays a market when an exact supported candidate symbol is found. If XAUTUSDT or OILUSD is not listed on your Delta Exchange account's public product catalogue, it will show as unavailable rather than using unrelated data.
- Signal calculations are based on Delta OHLC candle history and the Supertrend ATR 10 / 2.0 formula. The displayed direction is the latest calculated direction; Signal Price is the close of the most recent direction-change candle in the fetched history.
- This app is for information only and is not financial advice.
