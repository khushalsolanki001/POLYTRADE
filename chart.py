import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import db
from datetime import datetime

def generate_pnl_chart(user_id: int, output_path: str = "chart.png") -> bool:
    trades = db.get_trade_history(user_id)
    if not trades:
        return False
        
    times = []
    pnl_history = []
    current_pnl = 0.0
    
    # Simple cumulative progression
    for i, t in enumerate(trades):
        current_pnl += t["pnl"]
        times.append(i + 1)
        pnl_history.append(current_pnl)
        
    fig, ax = plt.subplots(figsize=(12, 7))
    
    color = '#2ecc71' if current_pnl >= 0 else '#e74c3c'
    ax.plot(times, pnl_history, label=f'Agent PnL (${current_pnl:+.2f})', color=color, linewidth=3, marker='o', markersize=6)
    
    # Base line
    ax.axhline(y=0, color='gray', linestyle='--', linewidth=1.5, alpha=0.6)
    
    # Styling grids
    ax.grid(True, alpha=0.3, linestyle=':')
    ax.set_xlabel('Total Trades Executed', fontsize=12, fontweight='bold')
    ax.set_ylabel('Cumulative PnL ($)', fontsize=12, fontweight='bold')
    
    wins = sum(1 for t in trades if t["pnl"] > 0)
    total_trades = len(trades)
    winrate = (wins / total_trades) * 100
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    ax.set_title(f'Agent Performance\n{now}', fontsize=16, fontweight='bold', pad=20)
    
    # Performance summary box
    stats_text = f"Trades Executed: {total_trades}  |  Win Rate: {winrate:.1f}%  |  Net PnL: ${current_pnl:+.2f}"
    ax.text(0.5, 0.02, stats_text, transform=ax.transAxes, ha='center', fontsize=11, 
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#f5f6fa', edgecolor='#dcdde1', alpha=0.9))
    
    ax.legend(loc='upper left', fontsize=12, framealpha=0.9, shadow=True)
    
    # Ensure layout fits boundaries
    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches='tight', facecolor='white')
    plt.close()
    
    return True
