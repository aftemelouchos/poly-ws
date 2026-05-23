"""CLOB v2 client factory — private key + funder (proxy/safe/eoa)."""

from __future__ import annotations

import logging

from py_clob_client_v2 import ApiCreds, ClobClient, SignatureTypeV2

from src.trading.config import TradingConfig

logger = logging.getLogger(__name__)


def create_clob_client(cfg: TradingConfig) -> ClobClient:
    if not cfg.private_key:
        raise ValueError("PRIVATE_KEY .env icinde tanimli olmali")
    if not cfg.funder_address:
        raise ValueError(
            "FUNDER_ADDRESS (veya POLY_FUNDER / PROXY_ADDRESS) .env icinde tanimli olmali"
        )

    sig = SignatureTypeV2(cfg.signature_type)
    temp = ClobClient(cfg.clob_host, key=cfg.private_key, chain_id=cfg.chain_id)
    creds = temp.create_or_derive_api_key()

    client = ClobClient(
        cfg.clob_host,
        key=cfg.private_key,
        chain_id=cfg.chain_id,
        creds=creds,
        signature_type=sig,
        funder=cfg.funder_address,
    )
    logger.info(
        "CLOB v2 client | signer=%s funder=%s signature_type=%s (%s)",
        client.get_address(),
        cfg.funder_address,
        int(sig),
        sig.name,
    )
    return client


def api_creds_from_client(client: ClobClient) -> ApiCreds:
    if not client.creds:
        raise RuntimeError("API credentials yok — once create_or_derive_api_key")
    return client.creds
