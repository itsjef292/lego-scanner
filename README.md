# LEGO Scanner

A local web app to scan brick parts with your phone camera, identify them via [Brickognize](https://brickognize.com), and log them to your [Rebrickable](https://rebrickable.com) parts list.

## How it works

1. Run the Flask server on your Mac
2. Open the local IP address on your phone
3. Take a photo of a LEGO part
4. The part is identified and color detected server-side via Brickognize
5. Confirm and add to your Rebrickable parts list

## Setup

1. Install dependencies:
   ```bash
   pip3 install flask requests python-dotenv
   ```

2. Copy `.env.example` to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   ```

3. Run the server:
   ```bash
   python3 app.py
   ```

4. Open the URL shown in the terminal on your phone (same Wi-Fi network required).

## Environment variables

| Variable | Description |
|---|---|
| `REBRICKABLE_API_KEY` | Your Rebrickable API key (from Account → Settings → API) |
| `REBRICKABLE_USER_TOKEN` | Your Rebrickable user token |
