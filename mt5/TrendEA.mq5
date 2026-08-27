//+------------------------------------------------------------------+
//|                                                      TrendEA.mq5  |
//|                  Validated LONG-ONLY trend strategy (Daily / D1)  |
//+------------------------------------------------------------------+
//
//  STRATEGY (attach to a DAILY / D1 chart)
//  ---------------------------------------
//  Direction : LONG ONLY (no shorts).
//
//  Entry     : Close > SMA(200)
//              AND MACD histogram > 0   (MACD main line - signal line)
//              AND (if UseADX) ADX(14) > AdxMin
//              All evaluated on the last CLOSED daily bar (shift 1).
//
//  Exit      : Close < SMA(200) on the last closed bar
//              -- the "SMA-recross" exit. We LET IT RUN: there is NO
//              fixed take-profit target.
//
//  Stop      : A hard protective stop-loss is placed 2 x ATR(14) below
//              the entry price. This is SAFETY ONLY -- the primary way
//              a trade ends is the SMA recross exit above.
//
//  Sizing    : Risk RiskPct % of account EQUITY per trade across the
//              2 x ATR stop distance. Lots are derived from the symbol's
//              REAL tick value / tick size read at runtime, so the SAME
//              EA sizes correctly on XAUUSD, US500, NAS100, etc.
//
//  GOLD vs INDEX toggle (UseADX)
//  -----------------------------
//  * GOLD  (XAUUSD)         -> UseADX = true   (ADX trend-strength gate)
//  * S&P / Nasdaq (US500 /  -> UseADX = false  (no ADX gate)
//    NAS100 / US30 etc.)
//  The equity indices validated better WITHOUT the ADX filter; gold
//  validated better WITH it. Set UseADX per instrument accordingly.
//
//  Timeframe : DAILY (D1). Attach the EA to a D1 chart.
//+------------------------------------------------------------------+
#property copyright "trading_admin"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>

//--- inputs -------------------------------------------------------------------
input int    SmaLen       = 200;         // SMA length (trend filter)
input int    MacdFast     = 12;          // MACD fast EMA
input int    MacdSlow     = 26;          // MACD slow EMA
input int    MacdSignal   = 9;           // MACD signal SMA
input bool   UseADX       = true;        // GOLD=true, S&P/Nasdaq=false
input int    AdxLen       = 14;          // ADX period
input double AdxMin       = 25.0;        // Minimum ADX to allow entry
input int    AtrLen       = 14;          // ATR period (stop distance)
input double AtrStopMult  = 2.0;         // Protective stop = mult x ATR
input double RiskPct      = 2.0;         // % of equity risked per trade
input long   MagicNumber  = 20260827;    // EA magic number
input string TradeComment = "TrendEA";   // Order comment

//--- globals ------------------------------------------------------------------
CTrade   trade;

int      hSMA  = INVALID_HANDLE;
int      hMACD = INVALID_HANDLE;
int      hADX  = INVALID_HANDLE;
int      hATR  = INVALID_HANDLE;

datetime g_lastBarTime = 0;              // last processed bar time

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetTypeFillingBySymbol(_Symbol);

   hSMA  = iMA(_Symbol, _Period, SmaLen, 0, MODE_SMA, PRICE_CLOSE);
   hMACD = iMACD(_Symbol, _Period, MacdFast, MacdSlow, MacdSignal, PRICE_CLOSE);
   hADX  = iADX(_Symbol, _Period, AdxLen);
   hATR  = iATR(_Symbol, _Period, AtrLen);

   if(hSMA  == INVALID_HANDLE ||
      hMACD == INVALID_HANDLE ||
      hADX  == INVALID_HANDLE ||
      hATR  == INVALID_HANDLE)
     {
      Print("OnInit: failed to create one or more indicator handles.");
      return(INIT_FAILED);
     }

   Print("TrendEA initialised on ", _Symbol, " ", EnumToString((ENUM_TIMEFRAMES)_Period),
         "  UseADX=", (UseADX ? "true" : "false"), "  RiskPct=", RiskPct);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(hSMA  != INVALID_HANDLE) IndicatorRelease(hSMA);
   if(hMACD != INVALID_HANDLE) IndicatorRelease(hMACD);
   if(hADX  != INVALID_HANDLE) IndicatorRelease(hADX);
   if(hATR  != INVALID_HANDLE) IndicatorRelease(hATR);
  }

//+------------------------------------------------------------------+
//| Return true once per newly completed bar                         |
//+------------------------------------------------------------------+
bool IsNewBar()
  {
   datetime t = iTime(_Symbol, _Period, 0);
   if(t == 0)
      return(false);                     // no data yet
   if(t == g_lastBarTime)
      return(false);                     // same bar, already processed
   g_lastBarTime = t;
   return(true);
  }

//+------------------------------------------------------------------+
//| Do we already own a position on this symbol (our magic)?         |
//+------------------------------------------------------------------+
bool HaveOurPosition()
  {
   if(!PositionSelect(_Symbol))
      return(false);
   if((long)PositionGetInteger(POSITION_MAGIC) != MagicNumber)
      return(false);
   return(true);
  }

//+------------------------------------------------------------------+
//| Read one indicator value from the last CLOSED bar (shift 1)      |
//| Returns true on success.                                         |
//+------------------------------------------------------------------+
bool CopyClosed(const int handle, const int buffer, double &value)
  {
   double arr[];
   ArraySetAsSeries(arr, true);
   // start=1 (last closed bar), count=1
   if(CopyBuffer(handle, buffer, 1, 1, arr) != 1)
      return(false);
   value = arr[0];
   return(true);
  }

//+------------------------------------------------------------------+
//| Compute lot size for the given stop distance (price units)       |
//+------------------------------------------------------------------+
double ComputeLots(const double stopDistPrice)
  {
   double volMin  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double volMax  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double volStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   if(stopDistPrice <= 0.0 || volStep <= 0.0)
      return(0.0);

   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickValue <= 0.0 || tickSize <= 0.0)
     {
      Print("ComputeLots: invalid tick value/size, cannot size trade.");
      return(0.0);
     }

   // Money lost per 1.0 lot if price moves the full stop distance:
   //   (stopDistPrice / tickSize) ticks  *  tickValue per tick per lot
   double lossPerLot = (stopDistPrice / tickSize) * tickValue;
   if(lossPerLot <= 0.0)
      return(0.0);

   double equity    = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskMoney = equity * (RiskPct / 100.0);

   double lots = riskMoney / lossPerLot;

   // Normalize down to the volume step.
   lots = MathFloor(lots / volStep) * volStep;

   if(lots < volMin)
     {
      PrintFormat("ComputeLots: risk-based lot %.4f below min %.2f -> using min. "
                  "Actual risk is slightly HIGHER than target %.1f%%.",
                  lots, volMin, RiskPct);
      lots = volMin;
     }
   if(lots > volMax)
      lots = volMax;

   return(lots);
  }

//+------------------------------------------------------------------+
//| Expert tick                                                      |
//+------------------------------------------------------------------+
void OnTick()
  {
   // Act only once per completed bar.
   if(!IsNewBar())
      return;

   // Need enough history for the slowest indicator.
   int needBars = SmaLen + MacdSlow + AtrLen + 2;
   if(Bars(_Symbol, _Period) < needBars)
     {
      Print("Not enough history yet (", Bars(_Symbol, _Period), "/", needBars, ").");
      return;
     }

   // --- read the last CLOSED bar's close ---
   double closeArr[];
   ArraySetAsSeries(closeArr, true);
   if(CopyClose(_Symbol, _Period, 1, 1, closeArr) != 1)
     {
      Print("OnTick: failed to copy close price, skipping bar.");
      return;
     }
   double lastClose = closeArr[0];

   // --- read indicators on the last closed bar ---
   double sma, macdMain, macdSignalLine, adx, atr;
   // iMACD: buffer 0 = MAIN line, buffer 1 = SIGNAL line.
   if(!CopyClosed(hSMA,  0, sma)            ||
      !CopyClosed(hMACD, 0, macdMain)       ||
      !CopyClosed(hMACD, 1, macdSignalLine) ||
      !CopyClosed(hATR,  0, atr))
     {
      Print("OnTick: indicator copy failed, skipping bar.");
      return;
     }

   // MACD histogram = main - signal.
   double macdHist = macdMain - macdSignalLine;

   // ADX only needed if gated.
   adx = 0.0;
   if(UseADX)
     {
      // iADX: buffer 0 = MAIN ADX line.
      if(!CopyClosed(hADX, 0, adx))
        {
         Print("OnTick: ADX copy failed, skipping bar.");
         return;
        }
     }

   bool havepos = HaveOurPosition();

   //--- EXIT: SMA recross (Close < SMA) --------------------------------------
   if(havepos)
     {
      if(lastClose < sma)
        {
         if(!trade.PositionClose(_Symbol))
            PrintFormat("PositionClose failed: retcode=%d %s",
                        trade.ResultRetcode(), trade.ResultRetcodeDescription());
         else
            Print("Exited long: Close(", lastClose, ") < SMA(", sma, ").");
        }
      return; // one position per symbol; nothing else to do
     }

   //--- ENTRY: long only ------------------------------------------------------
   bool longSignal = (lastClose > sma) && (macdHist > 0.0);
   if(UseADX)
      longSignal = longSignal && (adx > AdxMin);

   if(!longSignal)
      return;

   if(atr <= 0.0)
     {
      Print("ATR not valid, skipping entry.");
      return;
     }

   double ask       = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(ask <= 0.0)
      return;

   double stopDist  = AtrStopMult * atr;
   double stopPrice = NormalizeDouble(ask - stopDist, _Digits);

   double lots = ComputeLots(stopDist);
   if(lots <= 0.0)
     {
      Print("Computed lots <= 0, skipping entry.");
      return;
     }

   // trade.Buy(volume, symbol, price=0 -> market, sl, tp=0 -> none, comment)
   if(!trade.Buy(lots, _Symbol, 0.0, stopPrice, 0.0, TradeComment))
      PrintFormat("Buy failed: retcode=%d %s",
                  trade.ResultRetcode(), trade.ResultRetcodeDescription());
   else
      PrintFormat("Entered long: lots=%.2f  entry~%.*f  SL=%.*f (%.1fxATR)  ADXgate=%s",
                  lots, _Digits, ask, _Digits, stopPrice, AtrStopMult,
                  (UseADX ? "on" : "off"));
  }
//+------------------------------------------------------------------+
