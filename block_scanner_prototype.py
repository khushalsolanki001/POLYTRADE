import asyncio
from web3 import Web3
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BlockScanner")

# Using a robust public RPC node without API keys
RPC_URL = "https://polygon.drpc.org"
def get_w3():
    return Web3(Web3.HTTPProvider(RPC_URL))

# The ERC1155 Contract for Polymarket Shares (CTF Exchange)
CTF_CONTRACT = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
# TransferSingle topic (operator, from, to, id, value)
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"

async def scan_blocks(tracked_wallets: list[str], start_block: int = None):
    """
    Sub-20s latency scanner matching `TransferSingle` events exactly on the block level.
    """
    w3 = get_w3()
    
    if not start_block:
        start_block = w3.eth.block_number - 10
        
    logger.info(f"Starting block scanner from block {start_block}")
    
    while True:
        try:
            latest_block = w3.eth.block_number
            if start_block > latest_block:
                await asyncio.sleep(2)
                continue
                
            logger.info(f"Scanning blocks {start_block} to {latest_block}")
            
            # Fetch events from the CTF contract
            logs = w3.eth.get_logs({
                'address': CTF_CONTRACT,
                'fromBlock': start_block,
                'toBlock': latest_block,
                'topics': [TRANSFER_SINGLE_TOPIC]
            })
            
            for log in logs:
                # topic[2] is FROM, topic[3] is TO
                _from = '0x' + log['topics'][2].hex()[-40:]
                _to   = '0x' + log['topics'][3].hex()[-40:]
                
                # Check if the trade involves any wallets we track
                wallet_match = None
                side = None
                
                if _to in tracked_wallets:
                    wallet_match = _to
                    side = "BUY"  # user received shares
                elif _from in tracked_wallets:
                    wallet_match = _from
                    side = "SELL" # user sent (or redeemed) shares
                    
                if wallet_match:
                    # Parse the ERC1155 data payload: id (32 bytes), value (32 bytes)
                    # We can decode the exact token the user interacted with right away!
                    token_id = int.from_bytes(log['data'][:32], 'big')
                    amount = int.from_bytes(log['data'][32:], 'big') / 1_000_000.0  # CTF has 6 decimals
                    tx_hash = log['transactionHash'].hex()
                    
                    logger.info(f"🚨 [INSTANT ALERT] Wallet {wallet_match} executed a {side}!")
                    logger.info(f"   Amount: {amount} shares at block {log['blockNumber']} (Tx: {tx_hash})")
                    logger.info(f"   Token ID (Asset ID): {token_id}")
                    # In full code, we use Gamma API here to get the Title and Outcome using the token_id
                    
            # Move the cursor forward
            start_block = latest_block + 1
            
            # Wait for the next Polygon block (~2.5 seconds)
            await asyncio.sleep(3)
            
        except Exception as e:
            logger.error(f"Scanner error: {e}")
            await asyncio.sleep(5)

# Example usage
if __name__ == "__main__":
    # Fake wallet to watch or test
    loop = asyncio.get_event_loop()
    loop.run_until_complete(scan_blocks(["0xd2d75a43ba5addf54b4194c7b8aa6db8a2b5e28a"]))
