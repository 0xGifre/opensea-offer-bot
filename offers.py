"""Create individual WETH offers for NFTs on OpenSea Base."""

from __future__ import annotations

import os
import json
import random
import secrets
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

load_dotenv()

API_BASE = "https://api.opensea.io/api/v2"
CHAIN = "base"
CHAIN_ID = 8453
WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
SEAPORT = Web3.to_checksum_address("0x0000000000000068F116a894984e2DB1123eB395")
OPENSEA_CONDUIT_KEY = "0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000"
# OpenSea's conduit key resolves to this conduit; ERC-20 approvals go to the conduit.
OPENSEA_CONDUIT = Web3.to_checksum_address("0x1E0049783F008A0085193E00003D00cd54003c71")
OPENSEA_FEE_RECIPIENT = Web3.to_checksum_address("0x0000a26b00c1f0df003000390027140000faa719")
OPENSEA_FEE_BPS = 100
ZERO = "0x0000000000000000000000000000000000000000"

ERC20_ABI = [
    {"type": "function", "name": "balanceOf", "stateMutability": "view", "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view", "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable", "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "deposit", "stateMutability": "payable", "inputs": [], "outputs": []},
]
SEAPORT_ABI = [{"type": "function", "name": "getCounter", "stateMutability": "view", "inputs": [{"name": "offerer", "type": "address"}], "outputs": [{"type": "uint256"}]}]
ERC165_ABI = [{"type": "function", "name": "supportsInterface", "stateMutability": "view", "inputs": [{"name": "interfaceId", "type": "bytes4"}], "outputs": [{"type": "bool"}]}]


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} in .env")
    return value


def configured_or_prompt(name: str, prompt: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    if sys.stdin.isatty():
        value = input(prompt).strip()
        if value:
            os.environ[name] = value
            return value
    raise ValueError(f"Set {name} in .env or enter it when prompted")


def settings(require_token_ids: bool = False):
    private_key = required("PRIVATE_KEY")
    api_key = required("OPENSEA_API_KEY")
    rpc_url = required("BASE_RPC_URL")
    contract = Web3.to_checksum_address(required("NFT_CONTRACT"))
    raw_token_ids = os.getenv("TOKEN_IDS", "").strip()
    token_ids = [x.strip() for x in raw_token_ids.split(",") if x.strip()]
    if require_token_ids and not token_ids:
        raise ValueError("Set TOKEN_IDS for manual mode")
    amount = Decimal(required("OFFER_AMOUNT_WETH"))
    if amount <= 0:
        raise ValueError("OFFER_AMOUNT_WETH must be greater than zero")
    expiry = int(os.getenv("OFFER_EXPIRATION_SECONDS", "86400"))
    if expiry < 300:
        raise ValueError("OFFER_EXPIRATION_SECONDS must be at least 300")
    return private_key, api_key, rpc_url, contract, token_ids, amount, expiry


def connect(rpc_url: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise ConnectionError("Could not connect to BASE_RPC_URL")
    if w3.eth.chain_id != CHAIN_ID:
        raise ValueError(f"RPC is on chain {w3.eth.chain_id}; Base requires {CHAIN_ID}")
    return w3


def ensure_weth_and_approval(w3: Web3, account, needed: int) -> None:
    weth = w3.eth.contract(address=WETH, abi=ERC20_ABI)
    balance = weth.functions.balanceOf(account.address).call()
    if balance < needed:
        shortfall = needed - balance
        eth_balance = w3.eth.get_balance(account.address)
        if eth_balance < shortfall:
            raise ValueError(
                f"Not enough WETH or ETH. Need {Web3.from_wei(needed, 'ether')} WETH; "
                f"current WETH is {Web3.from_wei(balance, 'ether')} and ETH is "
                f"{Web3.from_wei(eth_balance, 'ether')}"
            )
        tx = weth.functions.deposit().build_transaction({
            "from": account.address,
            "value": shortfall,
            "nonce": w3.eth.get_transaction_count(account.address),
            "chainId": CHAIN_ID,
            "gas": 100000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"Wrapping ETH to WETH: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt.status != 1:
            raise RuntimeError("WETH deposit transaction failed")

    allowance = weth.functions.allowance(account.address, OPENSEA_CONDUIT).call()
    if allowance < needed:
        tx = weth.functions.approve(OPENSEA_CONDUIT, 2**256 - 1).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "chainId": CHAIN_ID,
            "gas": 100000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"Approving Seaport for WETH: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt.status != 1:
            raise RuntimeError("WETH approval transaction failed")


def nft_item_type(w3: Web3, contract: str) -> int:
    nft = w3.eth.contract(address=contract, abi=ERC165_ABI)
    try:
        if nft.functions.supportsInterface("0xd9b67a26").call():  # ERC-1155
            return 3
        if nft.functions.supportsInterface("0x80ac58cd").call():  # ERC-721
            return 2
    except Exception as exc:
        raise ValueError("Could not detect ERC-721/ERC-1155; contract must support ERC-165") from exc
    raise ValueError("NFT_CONTRACT does not report ERC-721 or ERC-1155 support")


def order_types():
    return {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "OfferItem": [
            {"name": "itemType", "type": "uint8"}, {"name": "token", "type": "address"},
            {"name": "identifierOrCriteria", "type": "uint256"}, {"name": "startAmount", "type": "uint256"},
            {"name": "endAmount", "type": "uint256"},
        ],
        "ConsiderationItem": [
            {"name": "itemType", "type": "uint8"}, {"name": "token", "type": "address"},
            {"name": "identifierOrCriteria", "type": "uint256"}, {"name": "startAmount", "type": "uint256"},
            {"name": "endAmount", "type": "uint256"}, {"name": "recipient", "type": "address"},
        ],
        "OrderComponents": [
            {"name": "offerer", "type": "address"}, {"name": "zone", "type": "address"},
            {"name": "offer", "type": "OfferItem[]"}, {"name": "consideration", "type": "ConsiderationItem[]"},
            {"name": "orderType", "type": "uint8"}, {"name": "startTime", "type": "uint256"},
            {"name": "endTime", "type": "uint256"}, {"name": "zoneHash", "type": "bytes32"},
            {"name": "salt", "type": "uint256"}, {"name": "conduitKey", "type": "bytes32"},
            {"name": "counter", "type": "uint256"},
        ],
    }


def make_and_post_offer(w3: Web3, account, api_key: str, contract: str, token_id: str,
                        amount_wei: int, expiry_seconds: int, item_type: int) -> dict:
    now = int(time.time())
    counter = w3.eth.contract(address=SEAPORT, abi=SEAPORT_ABI).functions.getCounter(account.address).call()
    # OpenSea's item-offer format keeps the full bid in `offer` and adds its
    # 1% fee as a separate WETH consideration item alongside the NFT.
    fee_wei = max(1, amount_wei * OPENSEA_FEE_BPS // 10_000)
    parameters = {
        "offerer": account.address,
        "zone": ZERO,
        "offer": [{
            "itemType": 1, "token": WETH, "identifierOrCriteria": "0",
            "startAmount": str(amount_wei), "endAmount": str(amount_wei),
        }],
        "consideration": [
            {
                "itemType": item_type, "token": contract, "identifierOrCriteria": str(int(token_id)),
                "startAmount": "1", "endAmount": "1", "recipient": account.address,
            },
            {
                "itemType": 1, "token": WETH, "identifierOrCriteria": "0",
                "startAmount": str(fee_wei), "endAmount": str(fee_wei), "recipient": OPENSEA_FEE_RECIPIENT,
            },
        ],
        "orderType": 0,
        "startTime": str(now),
        "endTime": str(now + expiry_seconds),
        "zoneHash": "0x" + "00" * 32,
        "salt": str(secrets.randbits(256)),
        "conduitKey": OPENSEA_CONDUIT_KEY,
        "counter": str(counter),
    }
    typed_data = {
        "types": order_types(),
        "primaryType": "OrderComponents",
        "domain": {"name": "Seaport", "version": "1.6", "chainId": CHAIN_ID, "verifyingContract": SEAPORT},
        "message": parameters,
    }
    signature = Account.sign_message(encode_typed_data(full_message=typed_data), account.key).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature

    response = requests.post(
        f"{API_BASE}/orders/{CHAIN}/seaport/offers",
        headers={"x-api-key": api_key, "accept": "application/json", "content-type": "application/json"},
        json={"parameters": parameters, "protocol_address": SEAPORT, "signature": signature},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"OpenSea HTTP {response.status_code}: {response.text[:1000]}")
    return response.json()


STATE_PATH = Path(os.getenv("STATE_FILE", "offer_state.json"))


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"activity_seen": [], "activity_after": 0, "random_attempted": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        data.setdefault("activity_seen", [])
        data.setdefault("activity_after", 0)
        data.setdefault("random_attempted", [])
        return data
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read state file {STATE_PATH}: {exc}") from exc


def save_state(state: dict) -> None:
    temp_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp_path.replace(STATE_PATH)


def opensea_get(api_key: str, url: str, params: dict) -> dict:
    response = requests.get(
        url,
        headers={"x-api-key": api_key, "accept": "application/json"},
        params=params,
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"OpenSea HTTP {response.status_code}: {response.text[:1000]}")
    return response.json()


def resolve_collection_slug(api_key: str, contract: str) -> str:
    configured = os.getenv("COLLECTION_SLUG", "").strip()
    if configured:
        print(f"Using OpenSea collection slug from .env: {configured}")
        return configured
    url = f"{API_BASE}/chain/{CHAIN}/contract/{contract}"
    metadata = opensea_get(api_key, url, {})
    slug = metadata.get("collection")
    if not slug:
        raise ValueError(
            "OpenSea did not return a collection slug for this contract. "
            "Set COLLECTION_SLUG in .env (the part after /collection/ in the OpenSea URL)."
        )
    os.environ["COLLECTION_SLUG"] = str(slug)
    print(f"Detected OpenSea collection slug: {slug}")
    return str(slug)


def sale_event_fields(event: dict):
    nft = event.get("nft") or event.get("asset") or {}
    contract = nft.get("contract") or {}
    if isinstance(contract, dict):
        contract = contract.get("address") or ""
    token_id = nft.get("identifier") or nft.get("token_id")
    timestamp = int(event.get("event_timestamp") or event.get("created_date") or 0)
    tx = event.get("transaction") or ""
    order_hash = event.get("order_hash") or ""
    event_key = f"{order_hash}:{tx}:{contract}:{token_id}:{timestamp}"
    return str(contract).lower(), str(token_id) if token_id is not None else None, timestamp, event_key


def send_one_offer(w3: Web3, account, api_key: str, contract: str, token_id: str,
                   amount_wei: int, expiry: int, item_type: int) -> dict:
    fee_wei = max(1, amount_wei * OPENSEA_FEE_BPS // 10_000)
    ensure_weth_and_approval(w3, account, amount_wei + fee_wei)
    return make_and_post_offer(w3, account, api_key, contract, token_id, amount_wei, expiry, item_type)


def activity_batch(api_key: str, slug: str, contract: str, after: int, lookback: int) -> tuple[list, int]:
    url = f"{API_BASE}/events/collection/{quote(slug, safe='')}"
    cursor = None
    events = []
    max_timestamp = after
    params = {"event_type": "sale", "limit": 200, "after": max(0, after - 2)}
    while True:
        page_params = dict(params)
        if cursor:
            page_params["next"] = cursor
        body = opensea_get(api_key, url, page_params)
        page = body.get("asset_events") or []
        for event in page:
            event_contract, token_id, timestamp, event_key = sale_event_fields(event)
            if timestamp:
                max_timestamp = max(max_timestamp, timestamp)
            event_chain = str(event.get("chain") or CHAIN).lower()
            if event_chain == CHAIN and event_contract == contract.lower() and token_id is not None:
                events.append((timestamp, event_key, token_id))
        cursor = body.get("next")
        if not cursor:
            break
    # Ignore stale first-page history only on initial startup; subsequent polls use the checkpoint.
    cutoff = int(time.time()) - lookback if after == 0 else after - 2
    return sorted((e for e in events if e[0] >= cutoff), key=lambda x: x[0]), max_timestamp


def choose_mode() -> str:
    valid = {"manual", "activity", "random"}
    configured = os.getenv("RUN_MODE", "").strip().lower()
    if configured:
        if configured not in valid:
            raise ValueError("RUN_MODE must be manual, activity, or random")
        return configured
    if not sys.stdin.isatty():
        raise ValueError("Set RUN_MODE=manual, activity, or random when running non-interactively")
    print("Choose offer mode:\n  1. Manual token IDs\n  2. Offer after each collection sale (activity)\n  3. Random token IDs without repeats")
    choice = input("Mode [1-3]: ").strip()
    modes = {"1": "manual", "2": "activity", "3": "random"}
    if choice not in modes:
        raise ValueError("Choose 1, 2, or 3")
    return modes[choice]


def run_manual(w3, account, api_key, contract, token_ids, amount_wei, expiry, item_type):
    exposure = sum(amount_wei + max(1, amount_wei * OPENSEA_FEE_BPS // 10_000) for _ in token_ids)
    ensure_weth_and_approval(w3, account, exposure)
    for token_id in token_ids:
        try:
            result = make_and_post_offer(w3, account, api_key, contract, token_id, amount_wei, expiry, item_type)
            print(f"Offer created for #{token_id}: order_hash={result.get('order_hash', 'n/a')}")
        except Exception as exc:
            print(f"Failed for #{token_id}: {exc}", file=sys.stderr)


def poll_activity_once(w3, account, api_key, contract, slug, amount_wei, expiry, item_type, state) -> int:
    lookback = max(1, int(os.getenv("ACTIVITY_INITIAL_LOOKBACK_SECONDS", "120")))
    events, max_timestamp = activity_batch(api_key, slug, contract, int(state["activity_after"]), lookback)
    seen = set(state["activity_seen"])
    processed = 0
    for _timestamp, event_key, token_id in events:
        if event_key in seen:
            continue
        try:
            result = send_one_offer(w3, account, api_key, contract, token_id, amount_wei, expiry, item_type)
            print(f"Sale activity: offer created for #{token_id}: order_hash={result.get('order_hash', 'n/a')}")
        except Exception as exc:
            print(f"Sale activity: failed for #{token_id}: {exc}", file=sys.stderr)
        seen.add(event_key)
        processed += 1
        time.sleep(max(0.0, float(os.getenv("OFFER_DELAY_SECONDS", "1"))))
    state["activity_seen"] = list(seen)[-20000:]
    state["activity_after"] = max(int(state["activity_after"]), max_timestamp)
    save_state(state)
    print(f"Activity monitor: checked collection '{slug}' — {processed} new matching sale(s); next check in {os.getenv('ACTIVITY_POLL_SECONDS', '10')} sec.")
    return processed


def random_offer_once(w3, account, api_key, contract, amount_wei, expiry, item_type, state) -> bool:
    start_id = int(os.getenv("RANDOM_ID_START", "1"))
    end_id = int(configured_or_prompt("RANDOM_ID_END", "Highest token ID in the range (inclusive): "))
    if start_id < 0 or end_id < start_id:
        raise ValueError("RANDOM_ID_END must be greater than or equal to RANDOM_ID_START")
    attempted = set(str(x) for x in state["random_attempted"])
    remaining_count = end_id - start_id + 1 - sum(1 for x in attempted if start_id <= int(x) <= end_id)
    if remaining_count <= 0:
        print("Random mode: every token ID in the configured range has been attempted.")
        return False
    # Rejection sampling avoids storing/shuffling the entire range and persists without repeats.
    while True:
        token_id = str(random.randint(start_id, end_id))
        if token_id not in attempted:
            break
    # Persist before sending so a restart cannot issue the same token twice.
    state["random_attempted"] = list(attempted | {token_id})
    save_state(state)
    try:
        result = send_one_offer(w3, account, api_key, contract, token_id, amount_wei, expiry, item_type)
        print(f"Random offer created for #{token_id}: order_hash={result.get('order_hash', 'n/a')}")
    except Exception as exc:
        print(f"Random offer failed for #{token_id} (ID remains marked attempted): {exc}", file=sys.stderr)
    time.sleep(max(0.0, float(os.getenv("OFFER_DELAY_SECONDS", "1"))))
    return True


def run_dynamic(mode, w3, account, api_key, contract, amount_wei, expiry, item_type):
    state = load_state()
    slug = resolve_collection_slug(api_key, contract) if mode == "activity" else None
    if mode == "activity":
        # On first run, only scan a recent window instead of replaying collection history.
        if int(state["activity_after"]) == 0:
            state["activity_after"] = int(time.time()) - max(1, int(os.getenv("ACTIVITY_INITIAL_LOOKBACK_SECONDS", "120")))
            save_state(state)
    while True:
        if mode == "activity":
            try:
                poll_activity_once(w3, account, api_key, contract, slug, amount_wei, expiry, item_type, state)
            except Exception as exc:
                print(f"Activity poll error: {exc}", file=sys.stderr)
            time.sleep(max(1, int(os.getenv("ACTIVITY_POLL_SECONDS", "10"))))
        elif mode == "random":
            if not random_offer_once(w3, account, api_key, contract, amount_wei, expiry, item_type, state):
                return


def main() -> int:
    try:
        mode = choose_mode()
        private_key, api_key, rpc_url, contract, token_ids, amount, expiry = settings(require_token_ids=(mode == "manual"))
        w3 = connect(rpc_url)
        account = Account.from_key(private_key)
        amount_wei = int(amount * Decimal(10**18))
        item_type = nft_item_type(w3, contract)
        print(f"Wallet: {account.address}; Base; NFT contract: {contract}; mode={mode}")
        if mode == "manual":
            run_manual(w3, account, api_key, contract, token_ids, amount_wei, expiry, item_type)
        else:
            run_dynamic(mode, w3, account, api_key, contract, amount_wei, expiry, item_type)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
