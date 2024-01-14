import sys
from flask import Flask, request
from binance.client import Client
import pandas as pd
import requests
import traceback
import json
#from waitress import serve

app = Flask(__name__)


async def initializeClient(apiKey, apiSecret, isTest):
  client = None
  try:
    if ("yes" in isTest):
      client = Client(apiKey, apiSecret, tld="com", testnet=True)
    else:
      client = Client(apiKey, apiSecret)
      if (client is not None):
        print("API Client initialized successfully!")
      else:
        print("API Client failed to initialized!")

  except Exception as e:
    print('failed to initialize API, trying again... ', e)
    exc_type, exc_value, exc_tb = sys.exc_info()
    print(traceback.format_exception(exc_type, exc_value, exc_tb))

  return client


async def getAccountBalance(client):
  try:
    x = client.futures_account()
    df = pd.DataFrame(x['assets'])
    usdtDf = df[df["asset"] == "USDT"]["walletBalance"]
    usdtBalance = float(usdtDf.iloc[0])
    return usdtBalance
  except:
    print("There was an error retrieving balance")


async def getServerTime():
  try:
    response = requests.get('https://fapi.binance.com/fapi/v1/time')
    response.raise_for_status()
    serverTime = response.json()['serverTime']
  except requests.exceptions.RequestException as e:
    print("Error fetching server time:", e)
    return None

  return serverTime


async def getUsdtPriceForSymbol(symbol):
  try:
    params = {'symbol': symbol}
    response = requests.get('https://fapi.binance.com/fapi/v1/ticker/price',
                            params=params)
    response.raise_for_status()
    data = response.json()
    btcToUsdtPrice = data['price']
  except requests.exceptions.RequestException as e:
    print("Error fetching symbol price:", e)
    return None

  return btcToUsdtPrice


async def getMarketPrice(client, symbol):
  x = client.get_symbol_ticker(symbol=symbol)
  price = float(x["price"])
  return price


async def placeLimitOrder(client, aSymbol, aVolume, tpSlPrice, aSide, dbPath):
  try:
    client.futures_create_order(symbol=aSymbol,
                                side=aSide,
                                type=Client.FUTURE_ORDER_TYPE_LIMIT,
                                timeInForce=Client.TIME_IN_FORCE_GTC,
                                quantity=aVolume,
                                price=tpSlPrice)

    await callUpdatesAPI(aSymbol, "entry", dbPath)
  except Exception as e:
    print("There was an error entering the position: ", e)
    exc_type, exc_value, exc_tb = sys.exc_info()
    print(traceback.format_exception(exc_type, exc_value, exc_tb))


async def placeTakeProfitOrder(client, symbol, tpSlPrice, aVolume,
                               executionType, dbPath):
  try:
    if executionType == "BUY":
      await placeLimitOrder(client, symbol, aVolume, tpSlPrice,
                            Client.SIDE_SELL, dbPath)
    if executionType == "SELL":
      await placeLimitOrder(client, symbol, aVolume, tpSlPrice,
                            Client.SIDE_BUY, dbPath)

    await callUpdatesAPI(symbol, "entry", dbPath)
  except:
    await placeTakeProfitOrder(client, symbol, tpSlPrice, aVolume,
                               executionType, dbPath)


async def placeOrder(client, aSymbol, executionType, pReward, totalLotSize,
                     dbPath):
  try:
    noOfDecimals = 0
    serverTime = await getServerTime()
    timeStamp = int(serverTime) + 2000
    print("Server Time for ", aSymbol, " is: ", serverTime, " and timestamp is: ", timeStamp)
    currentSymbolPrice = await getUsdtPriceForSymbol(aSymbol)
    accountBalance = await getAccountBalance(client)
    amountToTrade = float(accountBalance) * 0.20 * totalLotSize if accountBalance is not None else 10.00  #This can be automated, the 0.10 is the percentage of the balance to enter with
    volumeToTrade = float(amountToTrade) / float(currentSymbolPrice)
    aSide = Client.SIDE_BUY if executionType == "BUY" else Client.SIDE_SELL
    aVolume = abs(round(volumeToTrade, noOfDecimals))
    client.futures_create_order(symbol=aSymbol,
                                side=aSide,
                                type=Client.FUTURE_ORDER_TYPE_MARKET,
                                quantity=aVolume,
                                timestamp=timeStamp)

    ticker = client.futures_symbol_ticker(symbol=aSymbol)
    price = float(ticker['price'])
    #Place the Take Profit Order as well - BUY for SELL and vice versa only if output returns and entry price
    tpSlPrice = price + float(
      pReward) if executionType == "BUY" else price - float(pReward)
    print("Entry Price is: ", price, " and TP/SL Price is: ", tpSlPrice)

    await placeTakeProfitOrder(client, aSymbol, tpSlPrice, aVolume, executionType, dbPath)
    #await callUpdatesAPI(aSymbol, "entry", dbPath)

  except Exception as e:
    print("There was an error entering the position: ", e)
    exc_type, exc_value, exc_tb = sys.exc_info()
    print(traceback.format_exception(exc_type, exc_value, exc_tb))


async def closePosition(client, aSymbol, executionType, positionType,
                        pairReward, totalLotSize, openPosition, dbPath):
  try:
    # Since the new trade coming will be in opposite direction
    serverTime = await getServerTime()
    timeStamp = int(serverTime) - 1000
    aSide = None
    aVolume = 0.0
    if float(openPosition['positionAmt']) > 0.0:
      aSide = Client.SIDE_SELL
      aVolume = abs(float(openPosition['positionAmt']))
    elif float(openPosition['positionAmt']) < 0.0:
      aSide = Client.SIDE_BUY
      aVolume = abs(float(openPosition['positionAmt']))

    print("Quantity to close is: ", aVolume)
    if (aSide is not None):
      client.futures_create_order(
        symbol=aSymbol,
        side=aSide,
        type=Client.FUTURE_ORDER_TYPE_MARKET,
        quantity=aVolume,
        #closePosition=True,
        timestamp=timeStamp)

      # Close all Pending Orders for this Position
      await closeOrders(client, aSymbol, dbPath)
      print("Open position for ", aSymbol, " has been closed successfully")

    if ("new" in positionType):
      #Open a new trade for a currency pair
      await placeOrder(client, aSymbol, executionType, pairReward,
                       totalLotSize, dbPath)

  except Exception as e:
    print("There was an error closing the position: ", e)
    exc_type, exc_value, exc_tb = sys.exc_info()
    print(traceback.format_exception(exc_type, exc_value, exc_tb))
    #Update Trade exit here meaning that the position has been closed either due to a tp or sl
    await callUpdatesAPI(aSymbol, "exit", dbPath)


async def closeOrders(client, symbol, dbPath):
  try:
    x = client.futures_get_open_orders(symbol=symbol)
    df = pd.DataFrame(x)
    for index in df.index:
      client.futures_cancel_order(symbol=symbol, orderId=df["orderId"][index])
    print("Open orders for ", symbol, " has been closed successfully")
    await callUpdatesAPI(symbol, "exit", dbPath)
  except Exception as e:
    print("There was an error closing orders: ", e)
    exc_type, exc_value, exc_tb = sys.exc_info()
    print(traceback.format_exception(exc_type, exc_value, exc_tb))


async def callUpdatesAPI(symbol, positionType, dbPath):
  try:
    #The server URL
    forwardURL = "http://192.236.198.184/updates"
    data = {'symbol': symbol, 'positionType': positionType, 'dbPath': dbPath}
    response = requests.post(forwardURL, json=data)
    if response.status_code == 200:
      print('Data forwarded successfully.')
    else:
      print('Failed to forward data.')
  except Exception as e:
    print("There was an error closing orders: ", e)
    exc_type, exc_value, exc_tb = sys.exc_info()
    print(traceback.format_exception(exc_type, exc_value, exc_tb))


@app.route('/crypto', methods=['POST', 'GET'])
async def cryptoAPI():

  try:
    if request.method == 'POST':
      print("Receiving data...")
      payload = request.data

      #TODO: Initialize all the data variables...
      response = payload.decode("utf-8")
      data = json.loads(response)
      apiKey = data['apiKey']
      apiSecret = data['apiSecret']
      isTest = data['isTest']
      symbol = data['symbol']
      executionType = data['executionType']
      positionType = data['positionType']
      pairReward = data['pairReward']
      totalLotSize = data['totalLotSize']
      dbPath = data['dbPath']

      #Initialize the client API
      client = await initializeClient(apiKey, apiSecret, isTest)
      if (client is not None):

        #TODO: Check that there is an open position for this symbol
        positions = client.futures_position_information(symbol=symbol)
        openPosition = positions[0]

        #TODO: If there is an open position then call this with the right value - "yes" or "no" for openPosition
        await closePosition(client, symbol, executionType, positionType,
                            pairReward, totalLotSize, openPosition, dbPath)

      return 'success', 200
    else:
      print("Get request")
      return 'success', 200
  except Exception as e:
    print("[X] Exception Occured : ", e)
    return 'failure', 500


@app.route('/', methods=['POST', 'GET'])
async def home():
  return 'API is live 101', 200


if __name__ == '__main__':
  app.run(host='0.0.0.0', port=7980)
