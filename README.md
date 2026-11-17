# OpenSea Base NFT offers (Python)

This script creates one item offer per token ID. Each offer is denominated in Base WETH. It uses the private key locally to sign a Seaport 1.6 EIP-712 order, then submits the signed order to OpenSea. It never sends the private key to OpenSea. Orders include OpenSea's current 1% marketplace fee as a separate consideration item and use the OpenSea conduit key.

At startup, choose `manual`, `activity`, or `random`. You can also set `RUN_MODE` in `.env` to skip the prompt. Activity mode watches OpenSea collection sale events and offers on the sold NFT. Random mode selects IDs in the configured inclusive range without repeats; it stops after every ID has been attempted.

## Setup

1. Install Python 3.10 or newer.
2. Create and activate a virtual environment, then install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in the Base RPC URL, private key, OpenSea API key, NFT contract, and bid amount. For activity mode, also set `COLLECTION_SLUG`. For random mode, set the inclusive `RANDOM_ID_START` and `RANDOM_ID_END` bounds.
4. Ensure the wallet has enough Base ETH for gas. The script wraps the required amount into WETH if its WETH balance is short, then approves Seaport to spend WETH. These are on-chain transactions that cost gas.
5. Run:

   ```bash
   python offers.py
   ```

## Configuration

- `TOKEN_IDS`: comma-separated NFT token IDs. The script creates an individual offer for each ID.
- `COLLECTION_SLUG`: required for activity mode; the collection's OpenSea slug.
- `RANDOM_ID_START` / `RANDOM_ID_END`: inclusive numeric ID range for random mode. This assumes token IDs are sequential and exist in that range; it does not query actual minted IDs.
- `STATE_FILE`: local JSON file storing sale event deduplication and random IDs already attempted. Keep it between restarts to preserve no-repeat behavior. Delete it only if you intentionally want to reset progress.
- `ACTIVITY_POLL_SECONDS`, `ACTIVITY_INITIAL_LOOKBACK_SECONDS`, `OFFER_DELAY_SECONDS`: polling and pacing settings.
- `OFFER_AMOUNT_WETH`: bid amount per NFT. OpenSea's 1% marketplace fee is added on top, so total WETH exposure is approximately `TOKEN_IDS count × offer amount × 1.01`.
- `OFFER_EXPIRATION_SECONDS`: order lifetime (minimum 300 seconds).
- `NFT_CONTRACT`: ERC-721 or ERC-1155 contract on Base.

Test with a small offer and one token first. Each offer is independently fillable, so ensure your wallet has funds for the number of simultaneous offers you expect to remain active. Offers remain live until accepted, cancelled, or expired. Keep `.env` private and use a wallet with only the funds you intend to offer.
