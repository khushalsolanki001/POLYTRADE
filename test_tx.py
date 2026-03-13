from web3 import Web3
w3 = Web3(Web3.HTTPProvider('https://polygon.drpc.org'))

# find a recent polymarket transaction or we can just fetch one from api.py output 
# if we just run Python to fetch trades from API and print the transaction_hash
