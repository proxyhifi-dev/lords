"""
Lords Bot — Advanced Math Engine  v4.0
=======================================
Quant analytics: BSM pricing, Greeks, EV, Kelly Criterion,
Sharpe/Sortino, ATR, drawdown analysis, position sizing.
All pure functions — no side effects.
"""
from __future__ import annotations
import math
import statistics
from dataclasses import dataclass
from typing import Sequence

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))

# ── BSM Pricing ──────────────────────────────────────────
def bsm_price(S,K,T,r,sigma,opt="CE") -> float:
    if T <= 0:
        intrinsic = max(S-K,0) if opt=="CE" else max(K-S,0)
        return max(intrinsic, 0.01)
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    if opt=="CE": return max(S*_ncdf(d1) - K*math.exp(-r*T)*_ncdf(d2), 0.01)
    return max(K*math.exp(-r*T)*_ncdf(-d2) - S*_ncdf(-d1), 0.01)

def calibrated_iv(dte: int) -> float:
    if dte<=1: return 0.07
    if dte<=2: return 0.09
    if dte<=3: return 0.11
    if dte<=5: return 0.13
    if dte<=7: return 0.15
    return 0.16

def bsm_ask(S,K,dte,opt,spread=2.0) -> float:
    T = max(dte/365, 1/365)
    return round(bsm_price(S,K,T,0.065,calibrated_iv(dte),opt)+spread, 2)

def bsm_bid(S,K,dte,opt,spread=2.0) -> float:
    T = max(dte/365, 1/365)
    return round(max(bsm_price(S,K,T,0.065,calibrated_iv(dte),opt)-spread, 0.01), 2)

@dataclass
class Greeks:
    delta: float; gamma: float; theta: float; vega: float; rho: float

def compute_greeks(S,K,T,r,sigma,opt="CE") -> Greeks:
    if T <= 0:
        return Greeks(1.0 if opt=="CE" else -1.0, 0,0,0,0)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*sqrtT)
    d2 = d1-sigma*sqrtT
    npd1 = math.exp(-0.5*d1**2)/math.sqrt(2*math.pi)
    delta = _ncdf(d1) if opt=="CE" else _ncdf(d1)-1
    gamma = npd1/(S*sigma*sqrtT)
    vega  = S*npd1*sqrtT/100
    th_a  = -(S*npd1*sigma)/(2*sqrtT) - r*K*math.exp(-r*T)*(_ncdf(d2) if opt=="CE" else _ncdf(-d2))
    theta = th_a/365
    rho   = (K*T*math.exp(-r*T)*(_ncdf(d2) if opt=="CE" else -_ncdf(-d2)))/100
    return Greeks(round(delta,4),round(gamma,6),round(theta,4),round(vega,4),round(rho,4))

# ── EV / Edge ────────────────────────────────────────────
def expected_value(wr,avg_win,avg_loss) -> float:
    return round(wr*avg_win + (1-wr)*avg_loss, 2)

def profit_factor(gross_wins,gross_losses) -> float:
    return round(abs(gross_wins/gross_losses), 3) if gross_losses else 9999.0

def reward_risk_ratio(avg_win,avg_loss) -> float:
    return round(avg_win/abs(avg_loss), 3) if avg_loss else 9999.0

# ── Kelly Criterion ──────────────────────────────────────
def kelly_fraction(wr,rr) -> float:
    if rr<=0: return 0.0
    k = (rr*wr - (1-wr))/rr
    return round(max(k,0.0), 4)

def half_kelly(wr,rr) -> float:
    return round(kelly_fraction(wr,rr)/2, 4)

def kelly_position_size(capital,wr,avg_win,avg_loss) -> float:
    if not avg_loss or abs(avg_loss) < 1e-9: return 0.0
    rr = avg_win/abs(avg_loss)
    k  = half_kelly(wr,rr)
    return round(capital*k, 2)

# ── Sharpe / Sortino ─────────────────────────────────────
def sharpe_ratio(returns, tpy=50.0, rfr=0.065) -> float:
    if len(returns)<2: return 0.0
    mu = statistics.mean(returns); sd = statistics.stdev(returns)
    if sd < 1e-8: return 0.0
    return round((mu - rfr/tpy)/sd*math.sqrt(tpy), 3)

def sortino_ratio(returns, tpy=50.0, rfr=0.065) -> float:
    if len(returns)<2: return 0.0
    mu  = statistics.mean(returns)
    neg = [r for r in returns if r<0]
    if len(neg)<2: return 0.0
    dsd = statistics.stdev(neg)
    if dsd < 1e-8: return 0.0
    return round((mu - rfr/tpy)/dsd*math.sqrt(tpy), 3)

# ── Drawdown ─────────────────────────────────────────────
@dataclass
class DrawdownStats:
    max_drawdown: float; max_drawdown_pct: float
    recovery_factor: float; calmar_ratio: float

def drawdown_analysis(pnl_series, capital=50000.0, months=6.0) -> DrawdownStats:
    if not pnl_series: return DrawdownStats(0,0,0,0)
    if capital <= 0: capital = 50000.0
    if months <= 0: months = 6.0
    equity=[0.0]
    for p in pnl_series: equity.append(equity[-1]+p)
    peak=equity[0]; max_dd=0.0
    for e in equity:
        peak=max(peak,e); dd=e-peak; max_dd=min(max_dd,dd)
    net=equity[-1]; ddp=abs(max_dd)/capital*100
    rec=net/abs(max_dd) if max_dd else 0
    calmar=(net/capital)*(12/months)*100/ddp if ddp>0 else 0
    return DrawdownStats(round(max_dd,2),round(ddp,2),round(rec,3),round(calmar,3))

# ── ATR ──────────────────────────────────────────────────
def atr(candles, period=14) -> float:
    if len(candles)<2: return 15.0
    trs=[]
    for i in range(1,len(candles)):
        h=candles[i]["high"]; l=candles[i]["low"]; pc=candles[i-1]["close"]
        trs.append(max(h-l,abs(h-pc),abs(l-pc)))
    n=min(period,len(trs))
    return round(sum(trs[-n:])/n, 2)

# ── Capital requirement ───────────────────────────────────
@dataclass
class CapitalRequirement:
    min_capital: float; recommended_capital: float
    margin_per_lot: float; premium_exposure: float

def capital_requirement(avg_premium=150.0,order_qty=65,
                        max_daily_loss=5000.0,max_drawdown=20000.0) -> CapitalRequirement:
    exp=avg_premium*order_qty
    margin=exp*1.5
    min_c=max_daily_loss*3
    rec_c=max(max_drawdown*3, min_c*2)
    return CapitalRequirement(round(min_c),round(rec_c),round(margin),round(exp))

# ── Full analytics ────────────────────────────────────────
@dataclass
class StrategyAnalytics:
    total_trades:int; win_rate:float; gross_pnl:float; net_pnl:float
    avg_win:float; avg_loss:float; profit_factor:float; reward_risk:float
    sharpe:float; sortino:float; max_drawdown:float; max_drawdown_pct:float
    calmar_ratio:float; kelly_fraction:float; half_kelly:float
    ev_per_trade:float; capital_min:float; capital_recommended:float

def full_analytics(pnl_series, capital=50000.0, brokerage=94.4) -> StrategyAnalytics:
    if not pnl_series: return StrategyAnalytics(*([0]*18))
    wins=[p for p in pnl_series if p>0]; losses=[p for p in pnl_series if p<=0]
    n=len(pnl_series); wr=len(wins)/n
    aw=sum(wins)/len(wins) if wins else 0
    al=sum(losses)/len(losses) if losses else 0
    gross=sum(pnl_series); net=gross-n*brokerage
    dd=drawdown_analysis(pnl_series,capital)
    rr=reward_risk_ratio(aw,al)
    pf=profit_factor(sum(wins),sum(losses))
    sh=sharpe_ratio(list(pnl_series)); so=sortino_ratio(list(pnl_series))
    kf=kelly_fraction(wr,rr if rr < 9999.0 else 999)
    hk=half_kelly(wr,rr if rr < 9999.0 else 999)
    ev=expected_value(wr,aw,al)
    cr=capital_requirement(max_drawdown=abs(dd.max_drawdown))
    return StrategyAnalytics(
        total_trades=n, win_rate=round(wr*100,1), gross_pnl=round(gross,2),
        net_pnl=round(net,2), avg_win=round(aw,2), avg_loss=round(al,2),
        profit_factor=round(pf,3), reward_risk=round(rr,3),
        sharpe=sh, sortino=so, max_drawdown=dd.max_drawdown,
        max_drawdown_pct=dd.max_drawdown_pct, calmar_ratio=dd.calmar_ratio,
        kelly_fraction=round(kf*100,1), half_kelly=round(hk*100,1),
        ev_per_trade=ev, capital_min=cr.min_capital,
        capital_recommended=cr.recommended_capital,
    )
