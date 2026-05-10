import json
import math
from typing import Dict, List, Tuple
from statistics import NormalDist
from datamodel import OrderDepth, TradingState, Order

# Velvetfruit and Options Strategy Parameters
GLOBAL_THETA = 5247.43
GLOBAL_SIGMA_STAT = 17.0
CIRCUIT_BREAKER_DEV = 68.0
VE_EDGE = 1.5
OPTION_EDGE = 1.0
DEFAULT_POS_LIMIT_VE = 250
DEFAULT_POS_LIMIT_OPT = 500

# Hydrogel Pack Strategy Parameters
HYDRO_LIMIT = 250
HYDRO_WINDOW = 350
HYDRO_TAKE_THRESHOLD = 2.0

_PHI = NormalDist()

def norm_cdf(x: float) -> float:
    return _PHI.cdf(x)

def norm_pdf(x: float) -> float:
    return _PHI.pdf(x)

def ou_call_fair_value(strike: float, theta: float, sigma_stat: float) -> float:
    if sigma_stat <= 0:
        return max(theta - strike, 0.0)
    d = (theta - strike) / sigma_stat
    price = (theta - strike) * norm_cdf(d) + sigma_stat * norm_pdf(d)
    return max(price, 0.0)

class Trader:
    def get_mid_price(self, product: str, state: TradingState) -> float:
        if product not in state.order_depths:
            return None
        depth = state.order_depths[product]
        if not depth.buy_orders or not depth.sell_orders:
            return None
        best_bid = max(depth.buy_orders.keys())
        best_ask = min(depth.sell_orders.keys())
        return (best_bid + best_ask) / 2.0

    def run(self, state: TradingState) -> Tuple[Dict[str, List[Order]], int, str]:
        orders = {}
        conversions = 0
        
        # State management for history and persistence
        if state.traderData == "" or state.traderData == "OU_GLOBAL_STATIONARY":
            trader_data_dict = {"hydro_history": []}
        else:
            try:
                trader_data_dict = json.loads(state.traderData)
            except:
                trader_data_dict = {"hydro_history": []}
        
        # Hydrogel Pack Strategy Implementation
        hydro_product = "HYDROGEL_PACK"
        if hydro_product in state.order_depths:
            hydro_depth = state.order_depths[hydro_product]
            sell_orders = sorted(hydro_depth.sell_orders.items())
            buy_orders = sorted(hydro_depth.buy_orders.items(), reverse=True)
            
            if sell_orders and buy_orders:
                best_ask, ask_vol = sell_orders[0]
                best_bid, bid_vol = buy_orders[0]
                mid_price = (best_ask + best_bid) / 2
                
                history = trader_data_dict.get("hydro_history", [])
                history.append(mid_price)
                if len(history) > HYDRO_WINDOW:
                    history.pop(0)
                trader_data_dict["hydro_history"] = history
                
                if len(history) == HYDRO_WINDOW:
                    avg = sum(history) / HYDRO_WINDOW
                    variance = sum((x - avg)**2 for x in history) / HYDRO_WINDOW
                    std = max(variance**0.5, 1.0)
                    z_score = (mid_price - avg) / std
                    
                    current_pos = state.position.get(hydro_product, 0)
                    hydro_orders = []
                    
                    # Execution logic using taker and maker modes
                    if z_score < -HYDRO_TAKE_THRESHOLD:
                        qty = HYDRO_LIMIT - current_pos
                        if qty > 0:
                            take_qty = min(qty, abs(ask_vol))
                            hydro_orders.append(Order(hydro_product, best_ask, int(take_qty)))
                    elif z_score > HYDRO_TAKE_THRESHOLD:
                        qty = -HYDRO_LIMIT - current_pos
                        if qty < 0:
                            take_qty = max(qty, -bid_vol)
                            hydro_orders.append(Order(hydro_product, best_bid, int(take_qty)))
                    else:
                        if z_score < -0.5:
                            target_buy_price = best_bid + 1 if (best_ask - best_bid) > 1 else best_bid
                            buy_qty = HYDRO_LIMIT - current_pos
                            if buy_qty > 0:
                                hydro_orders.append(Order(hydro_product, target_buy_price, int(buy_qty // 4)))
                        if z_score > 0.5:
                            target_sell_price = best_ask - 1 if (best_ask - best_bid) > 1 else best_ask
                            sell_qty = -HYDRO_LIMIT - current_pos
                            if sell_qty < 0:
                                hydro_orders.append(Order(hydro_product, target_sell_price, int(sell_qty // 4)))
                    
                    if hydro_orders:
                        orders[hydro_product] = hydro_orders

        # Velvetfruit and Options Strategy Implementation
        ve_mid = self.get_mid_price("VELVETFRUIT_EXTRACT", state)
        if ve_mid is not None:
            deviation = abs(ve_mid - GLOBAL_THETA)
            
            # Local circuit breaker for Velvetfruit related products
            if deviation <= CIRCUIT_BREAKER_DEV:
                # Trade Underlying
                ve_depth = state.order_depths.get("VELVETFRUIT_EXTRACT")
                if ve_depth:
                    ve_orders = []
                    curr_pos = state.position.get("VELVETFRUIT_EXTRACT", 0)
                    for ask_price, ask_vol in sorted(ve_depth.sell_orders.items()):
                        if ask_price < GLOBAL_THETA - VE_EDGE:
                            buy_vol = min(-ask_vol, DEFAULT_POS_LIMIT_VE - curr_pos)
                            if buy_vol > 0:
                                ve_orders.append(Order("VELVETFRUIT_EXTRACT", ask_price, buy_vol))
                                curr_pos += buy_vol
                    curr_pos = state.position.get("VELVETFRUIT_EXTRACT", 0)
                    for bid_price, bid_vol in sorted(ve_depth.buy_orders.items(), reverse=True):
                        if bid_price > GLOBAL_THETA + VE_EDGE:
                            sell_vol = max(-bid_vol, -DEFAULT_POS_LIMIT_VE - curr_pos)
                            if sell_vol < 0:
                                ve_orders.append(Order("VELVETFRUIT_EXTRACT", bid_price, sell_vol))
                                curr_pos += sell_vol
                    if ve_orders:
                        orders["VELVETFRUIT_EXTRACT"] = ve_orders

                # Trade Options
                for product in state.order_depths:
                    if not product.startswith("VEV_"):
                        continue
                    try:
                        strike = float(product.split("_")[1])
                    except ValueError:
                        continue
                    
                    dynamic_edge = OPTION_EDGE
                    if strike >= 5400:
                        dynamic_edge = OPTION_EDGE * 2.0 
                    
                    fv = ou_call_fair_value(strike, GLOBAL_THETA, GLOBAL_SIGMA_STAT)
                    opt_depth = state.order_depths[product]
                    opt_orders = []
                    curr_pos = state.position.get(product, 0)
                    for ask_price, ask_vol in sorted(opt_depth.sell_orders.items()):
                        if ask_price < fv - dynamic_edge:
                            buy_vol = min(-ask_vol, DEFAULT_POS_LIMIT_OPT - curr_pos)
                            if buy_vol > 0:
                                opt_orders.append(Order(product, ask_price, buy_vol))
                                curr_pos += buy_vol
                    curr_pos = state.position.get(product, 0)
                    for bid_price, bid_vol in sorted(opt_depth.buy_orders.items(), reverse=True):
                        if bid_price > fv + dynamic_edge:
                            sell_vol = max(-bid_vol, -DEFAULT_POS_LIMIT_OPT - curr_pos)
                            if sell_vol < 0:
                                opt_orders.append(Order(product, bid_price, sell_vol))
                                curr_pos += sell_vol
                    if opt_orders:
                        orders[product] = opt_orders

        return orders, conversions, json.dumps(trader_data_dict)