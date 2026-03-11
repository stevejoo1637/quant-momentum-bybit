from bybit_api import BybitAPI
api = BybitAPI("30rtFZqmVMCQTFgM90", "p7AUjLkKApPP2tFTj3h1qBig5JKA7hKvWqI6")
r = api.session.get_wallet_balance(accountType="UNIFIED")
for acct in r["result"]["list"]:
    print("totalEquity:", acct.get("totalEquity"))
    print("totalAvailableBalance:", acct.get("totalAvailableBalance"))
    for coin in acct.get("coin", []):
        eq = coin.get("equity", "0")
        if eq and float(eq) > 0:
            print(f"  {coin['coin']}: equity={eq}, available={coin.get('availableToWithdraw','?')}, wallet={coin.get('walletBalance','?')}")
