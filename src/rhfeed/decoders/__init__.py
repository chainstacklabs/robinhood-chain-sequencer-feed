"""One module per protocol. Importing this package fills `intents.DECODERS`."""

from . import (
    erc20,  # noqa: F401
    uniswap,  # noqa: F401
    universal_router,  # noqa: F401
)
