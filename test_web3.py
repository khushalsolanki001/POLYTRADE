import time
from web3 import Web3

RPC = 'https://polygon-rpc.com/'
w3 = Web3(Web3.HTTPProvider(RPC))

if not w3.is_connected():
    print("Failed to connect")
    exit(1)

# A proxy wallet
wallet = "0x7b235aa8730fa67f815695746738fb14f7ce1efe"
wallet_padded = '0x000000000000000000000000' + wallet[2:]

print("Latest block:", w3.eth.block_number)

# Look for TransferSingle (Conditional Tokens)
# topic[0] = TransferSingle(address operator, address from, address to, uint256 id, uint256 value)
topic_transfer_single = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"

try:
    logs_from = w3.eth.get_logs({
        'fromBlock': w3.eth.block_number - 1000,
        'toBlock': 'latest',
        'address': '0x4D97DCd97eC945f40cF65F87097ACe5EA0476045', # CTF
        'topics': [topic_transfer_single, None, wallet_padded]
    })
    
    logs_to = w3.eth.get_logs({
        'fromBlock': w3.eth.block_number - 1000,
        'toBlock': 'latest',
        'address': '0x4D97DCd97eC945f40cF65F87097ACe5EA0476045',
        'topics': [topic_transfer_single, None, None, wallet_padded]
    })
    
    print("Logs from:", len(logs_from))
    print("Logs to:", len(logs_to))
    
    if logs_to:
        print("Latest log 'to' details:", logs_to[-1])
except Exception as e:
    print("Error:", e)
