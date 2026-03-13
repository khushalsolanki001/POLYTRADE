from web3 import Web3
w3 = Web3(Web3.HTTPProvider('https://polygon.drpc.org'))

# The Conditional Token Framework (CTF) contract emits TransferSingle and TransferBatch for Polymarket Shares.
CTF_CONTRACT = '0x4D97DCd97eC945f40cF65F87097ACe5EA0476045'
try:
    # Get latest block
    latest = w3.eth.block_number
    # Fetch TransferSingle logs from the last 100 blocks
    logs = w3.eth.get_logs({
        'address': CTF_CONTRACT,
        'fromBlock': latest - 5,
        'toBlock': latest, 
        'topics': ['0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62'] # TransferSingle
    })
    print(f"Found {len(logs)} TransferSingle events in the last 5 blocks.")
    if logs:
        # Example log
        log = logs[0]
        # Topics:
        # topic 0: Signature
        # topic 1: Operator
        # topic 2: From
        # topic 3: To
        _from = w3.to_hex(log['topics'][2])[-40:]
        _to = w3.to_hex(log['topics'][3])[-40:]
        print(f"First event: from=0x{_from} to=0x{_to} tx={log['transactionHash'].hex()}")
except Exception as e:
    print(f"Error: {e}")
