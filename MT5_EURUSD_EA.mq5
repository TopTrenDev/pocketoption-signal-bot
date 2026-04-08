#property strict
#property version   "1.00"
#property description "EURUSD EA template: EMA+RSI with daily stop, spread filter, SL/TP."

#include <Trade/Trade.mqh>

input string InpSymbol = "EURUSD";
input ENUM_TIMEFRAMES InpTimeframe = PERIOD_M5;
input double InpLotSize = 0.01;

input int InpFastEMA = 20;
input int InpSlowEMA = 50;
input int InpRSIPeriod = 14;
input double InpBuyRSIMin = 52.0;
input double InpSellRSIMax = 48.0;

input int InpSLPoints = 300;           // 30 pips on 5-digit symbols
input int InpTPPoints = 450;           // 45 pips on 5-digit symbols
input int InpMaxSpreadPoints = 25;     // 2.5 pips on 5-digit symbols
input bool InpOnePositionAtATime = true;
input ulong InpMagic = 20260401;

input bool InpUseDailyProfitStop = true;
input bool InpUseDailyLossStop = true;
input double InpDailyProfitPercent = 2.0; // stop trading for day once reached
input double InpDailyLossPercent = 2.0;   // stop trading for day once reached

CTrade trade;

int hFastEMA = INVALID_HANDLE;
int hSlowEMA = INVALID_HANDLE;
int hRSI = INVALID_HANDLE;

datetime lastBarTime = 0;
datetime dayAnchor = 0;
double dayStartBalance = 0.0;

bool IsNewBar()
{
   datetime t[1];
   if(CopyTime(InpSymbol, InpTimeframe, 0, 1, t) != 1)
      return false;

   if(t[0] != lastBarTime)
   {
      lastBarTime = t[0];
      return true;
   }
   return false;
}

void ResetDayAnchorIfNeeded()
{
   MqlDateTime nowStruct;
   TimeToStruct(TimeCurrent(), nowStruct);
   nowStruct.hour = 0;
   nowStruct.min = 0;
   nowStruct.sec = 0;
   datetime todayMidnight = StructToTime(nowStruct);

   if(dayAnchor != todayMidnight)
   {
      dayAnchor = todayMidnight;
      dayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
      Print("New day anchor set. Start balance: ", DoubleToString(dayStartBalance, 2));
   }
}

bool DailyStopReached()
{
   ResetDayAnchorIfNeeded();
   if(dayStartBalance <= 0.0)
      return false;

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double pnlPct = ((balance - dayStartBalance) / dayStartBalance) * 100.0;

   if(InpUseDailyProfitStop && pnlPct >= InpDailyProfitPercent)
   {
      Print("Daily profit stop reached: ", DoubleToString(pnlPct, 2), "%");
      return true;
   }

   if(InpUseDailyLossStop && pnlPct <= -InpDailyLossPercent)
   {
      Print("Daily loss stop reached: ", DoubleToString(pnlPct, 2), "%");
      return true;
   }

   return false;
}

bool SpreadOK()
{
   long spreadPoints = SymbolInfoInteger(InpSymbol, SYMBOL_SPREAD);
   if(spreadPoints > InpMaxSpreadPoints)
   {
      Print("Spread too high: ", spreadPoints, " points");
      return false;
   }
   return true;
}

bool HasOpenPosition()
{
   if(!PositionSelect(InpSymbol))
      return false;

   long magic = PositionGetInteger(POSITION_MAGIC);
   return (ulong)magic == InpMagic;
}

bool GetSignal(bool &buySignal, bool &sellSignal)
{
   buySignal = false;
   sellSignal = false;

   double fast[3], slow[3], rsi[3];
   ArraySetAsSeries(fast, true);
   ArraySetAsSeries(slow, true);
   ArraySetAsSeries(rsi, true);

   if(CopyBuffer(hFastEMA, 0, 0, 3, fast) < 3) return false;
   if(CopyBuffer(hSlowEMA, 0, 0, 3, slow) < 3) return false;
   if(CopyBuffer(hRSI, 0, 0, 3, rsi) < 3) return false;

   // Use closed bar values at index 1 for stable backtests/live consistency.
   bool trendUp = fast[1] > slow[1];
   bool trendDown = fast[1] < slow[1];

   buySignal = trendUp && rsi[1] >= InpBuyRSIMin;
   sellSignal = trendDown && rsi[1] <= InpSellRSIMax;
   return true;
}

void TryOpenTrade(bool buySignal, bool sellSignal)
{
   if(InpOnePositionAtATime && HasOpenPosition())
      return;

   double ask = SymbolInfoDouble(InpSymbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(InpSymbol, SYMBOL_BID);
   if(ask <= 0 || bid <= 0)
      return;

   int digits = (int)SymbolInfoInteger(InpSymbol, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(InpSymbol, SYMBOL_POINT);
   if(point <= 0.0)
      return;

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(10);

   if(buySignal && !sellSignal)
   {
      double sl = NormalizeDouble(ask - InpSLPoints * point, digits);
      double tp = NormalizeDouble(ask + InpTPPoints * point, digits);
      if(!trade.Buy(InpLotSize, InpSymbol, ask, sl, tp, "EMA_RSI_BUY"))
         Print("Buy failed: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
   }
   else if(sellSignal && !buySignal)
   {
      double sl = NormalizeDouble(bid + InpSLPoints * point, digits);
      double tp = NormalizeDouble(bid - InpTPPoints * point, digits);
      if(!trade.Sell(InpLotSize, InpSymbol, bid, sl, tp, "EMA_RSI_SELL"))
         Print("Sell failed: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
   }
}

int OnInit()
{
   if(_Symbol != InpSymbol)
      Print("Warning: attach EA to ", InpSymbol, " chart for intended behavior.");

   hFastEMA = iMA(InpSymbol, InpTimeframe, InpFastEMA, 0, MODE_EMA, PRICE_CLOSE);
   hSlowEMA = iMA(InpSymbol, InpTimeframe, InpSlowEMA, 0, MODE_EMA, PRICE_CLOSE);
   hRSI = iRSI(InpSymbol, InpTimeframe, InpRSIPeriod, PRICE_CLOSE);

   if(hFastEMA == INVALID_HANDLE || hSlowEMA == INVALID_HANDLE || hRSI == INVALID_HANDLE)
   {
      Print("Failed to create indicator handles.");
      return INIT_FAILED;
   }

   ResetDayAnchorIfNeeded();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(hFastEMA != INVALID_HANDLE) IndicatorRelease(hFastEMA);
   if(hSlowEMA != INVALID_HANDLE) IndicatorRelease(hSlowEMA);
   if(hRSI != INVALID_HANDLE) IndicatorRelease(hRSI);
}

void OnTick()
{
   if(_Symbol != InpSymbol)
      return;

   if(DailyStopReached())
      return;

   if(!SpreadOK())
      return;

   if(!IsNewBar())
      return;

   bool buySignal, sellSignal;
   if(!GetSignal(buySignal, sellSignal))
      return;

   TryOpenTrade(buySignal, sellSignal);
}
