import traceback
from web3 import Web3
rpcs = [
    "https://polygon.drpc.org",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon.meowrpc.com",
    "https://1rpc.io/matic"
]
for rpc in rpcs:
    try:
        w3 = Web3(Web3.HTTPProvider(rpc))
        print(f"Latest Block from {rpc}: {w3.eth.block_number}")
        break
    except Exception as e:
        print(f"Failed for {rpc}: {e}")
