export type JsonRow = Record<string, unknown>
export interface CandlePoint { time:number;open:number;high:number;low:number;close:number;volume:number|null;provider:string|null }
export interface TradeMarker { action:'ENTER'|'ADD'|'REDUCE'|'EXIT';timestamp:string;executionPrice:number;quantity:number|null;fees:number|null;slippage:number|null;strategy:string|null;reason:unknown;positionId:string|null;tradeId:string|null }

export const isRow=(value:unknown):value is JsonRow=>typeof value==='object'&&value!==null&&!Array.isArray(value)
const numberOrNull=(value:unknown)=>typeof value==='number'&&Number.isFinite(value)?value:null
const stringOrNull=(value:unknown)=>typeof value==='string'?value:null
const rows=(value:unknown):JsonRow[]=>Array.isArray(value)?value.filter(isRow):[]
const timestamp=(value:unknown)=>typeof value==='string'?Math.floor(Date.parse(value)/1000):NaN

export function mapCandles(payload:JsonRow):CandlePoint[]{
 const seen=new Set<number>();const mapped:CandlePoint[]=[]
 for(const row of rows(payload.candles)){
  const time=timestamp(row.opened_at);const open=numberOrNull(row.open);const high=numberOrNull(row.high);const low=numberOrNull(row.low);const close=numberOrNull(row.close)
  if(!Number.isFinite(time)||open===null||high===null||low===null||close===null||seen.has(time))continue
  seen.add(time);const source=isRow(row.source)?row.source:{}
  mapped.push({time,open,high,low,close,volume:numberOrNull(row.volume),provider:stringOrNull(source.provider)})
 }
 return mapped.sort((a,b)=>a.time-b.time)
}

const actions=new Set(['ENTER','ADD','REDUCE','EXIT'])
export function mapTradeMarkers(payload:JsonRow):TradeMarker[]{
 return rows(payload.markers).flatMap(row=>{
  const action=stringOrNull(row.action);const at=stringOrNull(row.timestamp);const price=numberOrNull(row.execution_price)
  if(!action||!actions.has(action)||!at||price===null)return []
  return [{action:action as TradeMarker['action'],timestamp:at,executionPrice:price,quantity:numberOrNull(row.quantity),fees:numberOrNull(row.fees),slippage:numberOrNull(row.slippage),strategy:stringOrNull(row.strategy),reason:row.reason,positionId:stringOrNull(row.position_id),tradeId:stringOrNull(row.trade_id)}]
 }).sort((a,b)=>a.timestamp.localeCompare(b.timestamp))
}

export const mapRows=(payload:JsonRow,key:string)=>rows(payload[key])
export const availableTimeframes=(payload:JsonRow)=>Array.isArray(payload.available_timeframes)?payload.available_timeframes.filter((v):v is string=>typeof v==='string'):[]
export const instrumentFromRow=(row:JsonRow)=>typeof row.instrument==='string'?row.instrument:typeof row.symbol==='string'?row.symbol:Array.isArray(row.instruments)&&typeof row.instruments[0]==='string'?row.instruments[0]:isRow(row.instrument)&&typeof row.instrument.symbol==='string'?row.instrument.symbol:undefined
export const probabilityLabel=(forecast:JsonRow)=>{
 const up=numberOrNull(forecast.probability_up);const down=numberOrNull(forecast.probability_down)
 return up!==null?`${(up*100).toFixed(1)}% up`:down!==null?`${(down*100).toFixed(1)}% down`:'Unavailable — not calibrated'
}
export const tradeSummary=(detail:JsonRow)=>{
 const trade=isRow(detail.trade)?detail.trade:{};return {
  gross:numberOrNull(trade.gross_pnl),fees:numberOrNull(trade.fees),slippage:numberOrNull(trade.slippage),
  net:numberOrNull(trade.realized_pnl),mfe:numberOrNull(trade.mfe),mae:numberOrNull(trade.mae),capture:numberOrNull(trade.exit_capture_ratio),
 }
}
