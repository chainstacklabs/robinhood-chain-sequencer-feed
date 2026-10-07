"""Names for the contracts `via` walks through. Display only; nothing dispatches on these.

Addresses verified on Robinhood Chain mainnet 2026-09-09 (chainstacklabs/fomo-solana-rh-listeners,
registry.json) unless noted. They go stale; a wrong label is cosmetic, a wrong decoder is
not, which is why decoding keys on selectors and this file does not.
"""

from __future__ import annotations

from .codec import addr

LABELS: dict[bytes, str] = {
    addr("0x4337084D9E255Ff0702461CF8895CE9E3b5Ff108"): "entrypoint_v08",
    addr("0x0000000071727De22E5E9d8BAf0edAc6f37da032"): "entrypoint_v07",
    addr("0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789"): "entrypoint_v06",
    addr("0xCcC88a9d1B4ED6b0EABA998850414b24f1c315bE"): "relay_router",
    addr("0xb92fe925DC43a0ECdE6c8b1a2709c170Ec4fFf4f"): "relay_executor",
    addr("0x4cD00E387622C35bDDB9b4c962C136462338BC31"): "relay_depository",
    addr("0xf70da97812cb96acdf810712aa562db8dfa3dbef"): "relay_solver_treasury",
    addr("0x0000000000001fF3684f28c67538d4D072C22734"): "allowance_holder",
    addr("0x39b38686a19836ac10162c490e4558e120cbbe5f"): "settler",
    # Seen as AllowanceHolder's target in tests/frames.jsonl, 2026-10-07; not in the registry.
    addr("0x1d4b86491ec211257cbedd77a4380a7494624eff"): "settler",
    addr("0xcaf681a66d020601342297493863e78c959e5cb2"): "swap_router02",
    addr("0x73991a25c818bf1f1128deaab1492d45638de0d3"): "v3_position_manager",
    addr("0x8366a39CC670B4001A1121B8F6A443A643e40951"): "v4_pool_manager",
    addr("0x1cbaF24D53fe930fCe8EFF149fA797D2611Da149"): "pons",
    addr("0x000000000022D473030F116dDEE9F6B43aC78BA3"): "permit2",
    addr("0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc"): "steps_router",
    addr("0xe492912f37c2a4eca45d42dc67548f4c6cd7ce2b"): "steps_router",
    addr("0x5b8d85ebabf17cf6b67bfa2fe6795623951cd70e"): "steps_router_v1",
    addr("0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"): "USDG",
    addr("0x0bd7d308f8e1639fab988df18a8011f41eacad73"): "WETH",
}


def label(address: bytes) -> str:
    return LABELS.get(address) or f"0x{address[:4].hex()}…"
