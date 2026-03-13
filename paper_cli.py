import argparse
import asyncio
import aiohttp
import sys
import re
from typing import Optional

# Import local DB functions
import db

GAMMA_API_URL = "https://gamma-api.polymarket.com/events?slug={slug}"
DEFAULT_USER_ID = 1  # CLI user

async def get_market_data(url: str) -> Optional[dict]:
    """Extract slug from URL and fetch market data from Polymarket API."""
    # Extract slug
    match = re.search(r'polymarket\.com/event/([^/?#]+)', url)
    if not match:
        print("Error: Could not extract event slug from URL. Make sure it looks like https://polymarket.com/event/<slug>")
        return None
    
    slug = match.group(1)
    
    async with aiohttp.ClientSession() as session:
        api_url = GAMMA_API_URL.format(slug=slug)
        async with session.get(api_url) as resp:
            if resp.status != 200:
                print(f"Error fetching API: HTTP {resp.status}")
                return None
            data = await resp.json()
            if not data or not isinstance(data, list):
                print("Error: Event not found on Polymarket API.")
                return None
            return data[0]

def find_outcome_index(outcomes: list[str], target: str) -> Optional[int]:
    """Find the index of the outcome (Yes/No, Up/Down, etc) ignoring case."""
    target_lower = target.lower()
    for i, o in enumerate(outcomes):
        if o.lower() == target_lower:
            return i
    return None

async def buy(args):
    """Handle the buy command: amount is in USD."""
    db.init_paper_user(DEFAULT_USER_ID)
    
    event = await get_market_data(args.url)
    if not event: return
    
    # We use the first market in the event for simplicity
    markets = event.get("markets", [])
    if not markets:
        print("Error: No markets found in this event.")
        return
    market = markets[0]
    
    import json
    outcomes = json.loads(market.get("outcomes", "[]"))
    outcome_prices = json.loads(market.get("outcomePrices", "[]"))
    
    idx = find_outcome_index(outcomes, args.outcome)
    if idx is None:
        print(f"Error: Outcome '{args.outcome}' not found. Available: {', '.join(outcomes)}")
        return
        
    price = float(outcome_prices[idx])
    if price <= 0 or price >= 1:
        print(f"Error: Invalid or stale price {price} for outcome '{outcomes[idx]}'.")
        return
        
    amount_usd = float(args.amount)
    shares_to_buy = amount_usd / price
    
    balance = db.get_paper_balance(DEFAULT_USER_ID)
    if balance < amount_usd:
        print(f"Error: Insufficient balance. You have ${balance:.2f}, but need ${amount_usd:.2f}")
        return
        
    # Deduct balance
    db.update_paper_balance(DEFAULT_USER_ID, balance - amount_usd)
    
    # Add position
    slug = market.get("slug", "unknown")
    title = market.get("question", "Unknown Market")
    outcome_str = outcomes[idx]
    
    existing = db.get_paper_position(DEFAULT_USER_ID, slug, outcome_str)
    
    if existing:
        current_shares = existing["shares"]
        current_avg = existing["avg_price"]
        # New weighted average cost
        total_cost = (current_shares * current_avg) + amount_usd
        new_shares = current_shares + shares_to_buy
        new_avg = total_cost / new_shares
    else:
        new_shares = shares_to_buy
        new_avg = price
        
    db.upsert_paper_position(DEFAULT_USER_ID, slug, title, outcome_str, new_shares, new_avg)
    
    print(f"✅ Success! Bought {shares_to_buy:.2f} shares of '{outcome_str}' in '{title}'")
    print(f"💰 Execution Price: ${price:.4f} | Total Cost: ${amount_usd:.2f}")
    print(f"💵 Remaining Virtual Balance: ${balance - amount_usd:.2f}")

async def sell(args):
    """Handle the sell command: amount can be shares or USD worth based on current price. We'll assume the input is shares for simplicity."""
    db.init_paper_user(DEFAULT_USER_ID)
    
    event = await get_market_data(args.url)
    if not event: return
    
    markets = event.get("markets", [])
    if not markets:
        print("Error: No markets found in this event.")
        return
    market = markets[0]
    
    import json
    outcomes = json.loads(market.get("outcomes", "[]"))
    outcome_prices = json.loads(market.get("outcomePrices", "[]"))
    
    idx = find_outcome_index(outcomes, args.outcome)
    if idx is None:
        print(f"Error: Outcome '{args.outcome}' not found. Available: {', '.join(outcomes)}")
        return
        
    price = float(outcome_prices[idx])
    if price <= 0 or price >= 1:
        print(f"Error: Invalid or stale price {price} for outcome '{outcomes[idx]}'.")
        return
        
    slug = market.get("slug", "unknown")
    outcome_str = outcomes[idx]
    
    position = db.get_paper_position(DEFAULT_USER_ID, slug, outcome_str)
    if not position or position["shares"] <= 0:
        print(f"Error: You do not own any shares of '{outcome_str}' in this market.")
        return
    
    shares_to_sell = float(args.shares)
    if shares_to_sell > position["shares"]:
        print(f"Error: You only own {position['shares']:.2f} shares, cannot sell {shares_to_sell:.2f}.")
        return
        
    proceeds_usd = shares_to_sell * price
    
    # Update balance
    balance = db.get_paper_balance(DEFAULT_USER_ID)
    db.update_paper_balance(DEFAULT_USER_ID, balance + proceeds_usd)
    
    # Update position
    remaining_shares = position["shares"] - shares_to_sell
    if remaining_shares < 0.0001: # Essentially 0
        db.remove_paper_position(position["id"])
    else:
        db.upsert_paper_position(DEFAULT_USER_ID, slug, position["market_title"], outcome_str, remaining_shares, position["avg_price"])
        
    profit_per_share = price - position["avg_price"]
    total_profit = profit_per_share * shares_to_sell
    
    print(f"✅ Success! Sold {shares_to_sell:.2f} shares of '{outcome_str}'.")
    print(f"💰 Execution Price: ${price:.4f} (Avg Cost: ${position['avg_price']:.4f})")
    print(f"💸 Proceeds: ${proceeds_usd:.2f} | PnL on Trade: ${total_profit:+.2f}")
    print(f"💵 New Virtual Balance: ${(balance + proceeds_usd):.2f}")

async def portfolio(args):
    """View the current portfolio and unrealized PnL."""
    db.init_paper_user(DEFAULT_USER_ID)
    
    balance = db.get_paper_balance(DEFAULT_USER_ID)
    positions = db.get_all_paper_positions(DEFAULT_USER_ID)
    
    print("\n" + "="*50)
    print(f"📜 VIRTUAL PORTFOLIO")
    print("="*50)
    print(f"💵 Available Cash: ${balance:.2f}\n")
    
    if not positions:
        print("You have no open positions.")
        return
        
    print("Fetching live prices for your active positions...\n")
    
    total_value = balance
    async with aiohttp.ClientSession() as session:
        for p in positions:
            api_url = GAMMA_API_URL.format(slug=p['market_slug'])
            async with session.get(api_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and isinstance(data, list):
                        market = data[0].get("markets", [])[0]
                        import json
                        outcomes = json.loads(market.get("outcomes", "[]"))
                        prices = json.loads(market.get("outcomePrices", "[]"))
                        
                        idx = find_outcome_index(outcomes, p['outcome'])
                        if idx is not None:
                            curr_price = float(prices[idx])
                            curr_val = curr_price * p['shares']
                            cost_basis = p['avg_price'] * p['shares']
                            pnl = curr_val - cost_basis
                            pnl_pct = (pnl / cost_basis) * 100 if cost_basis > 0 else 0
                            
                            print(f"• {p['market_title'][:60]}...")
                            print(f"  Outcome: {p['outcome']} | Shares: {p['shares']:.2f}")
                            print(f"  Avg Cost: ${p['avg_price']:.4f} | Current Price: ${curr_price:.4f}")
                            print(f"  Current Value: ${curr_val:.2f} | PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%)\n")
                            
                            total_value += curr_val
                            continue
            
            # Fallback if API fails
            val = p['avg_price'] * p['shares']
            print(f"• {p['market_title'][:60]}...")
            print(f"  Outcome: {p['outcome']} | Shares: {p['shares']:.2f}")
            print(f"  Avg Cost: ${p['avg_price']:.4f} | Current Price: UNKNOWN")
            print(f"  Current Value: (Stale) ${val:.2f}\n")
            total_value += val
            
    print("="*50)
    print(f"🏦 Total Portfolio Value: ${total_value:.2f}")
    print(f"📈 Total Gain/Loss: ${(total_value - 10000.0):+.2f}")
    print("="*50 + "\n")

def main():
    db.init_db() # ensure tables are created
    
    parser = argparse.ArgumentParser(description="Polymarket Paper Trading CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Buy command
    parser_buy = subparsers.add_parser("buy", help="Buy shares")
    parser_buy.add_argument("url", type=str, help="Polymarket event URL")
    parser_buy.add_argument("outcome", type=str, help="Outcome to buy (e.g. Yes, No, Up, Down)")
    parser_buy.add_argument("amount", type=str, help="Amount in USD (e.g. 10 or 10$)")
    
    # Sell command
    parser_sell = subparsers.add_parser("sell", help="Sell shares")
    parser_sell.add_argument("url", type=str, help="Polymarket event URL")
    parser_sell.add_argument("outcome", type=str, help="Outcome to sell (e.g. Yes, No)")
    parser_sell.add_argument("shares", type=str, help="Number of shares to sell")
    
    # Portfolio command
    parser_portfolio = subparsers.add_parser("portfolio", help="View your paper trading portfolio")
    
    args = parser.parse_args()
    
    # Clean up amount fields in case they have a $ sign
    if hasattr(args, 'amount') and args.amount:
        args.amount = args.amount.replace('$', '')
        
    if args.command == "buy":
        asyncio.run(buy(args))
    elif args.command == "sell":
        asyncio.run(sell(args))
    elif args.command == "portfolio":
        asyncio.run(portfolio(args))

if __name__ == "__main__":
    main()
