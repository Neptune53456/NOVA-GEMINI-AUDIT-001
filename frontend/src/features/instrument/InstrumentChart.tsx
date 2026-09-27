import { useEffect, useRef, useState } from 'react'
import { CandlestickSeries, ColorType, CrosshairMode, HistogramSeries, LineSeries, createChart, createSeriesMarkers, type SeriesMarker, type UTCTimestamp } from 'lightweight-charts'
import type { CandlePoint, JsonRow, TradeMarker } from './marketData'

interface Props { candles:CandlePoint[];markers:TradeMarker[];events:JsonRow[];patterns:JsonRow[];forecasts:JsonRow[];focusTimestamp?:string;showVolume:boolean;showTrades:boolean;showEvents:boolean;showPatterns:boolean;showForecasts:boolean;onSelectTrade:(marker:TradeMarker)=>void }
const markerStyle={ENTER:{color:'#22c55e',shape:'arrowUp',position:'belowBar'},ADD:{color:'#2dd4bf',shape:'circle',position:'belowBar'},REDUCE:{color:'#f59e0b',shape:'circle',position:'aboveBar'},EXIT:{color:'#ef4444',shape:'arrowDown',position:'aboveBar'}} as const

const at=(value:unknown)=>typeof value==='string'&&Number.isFinite(Date.parse(value))?(Date.parse(value)/1000) as UTCTimestamp:undefined
export function InstrumentChart({candles,markers,events,patterns,forecasts,focusTimestamp,showVolume,showTrades,showEvents,showPatterns,showForecasts,onSelectTrade}:Props){
 const container=useRef<HTMLDivElement>(null);const [hover,setHover]=useState<CandlePoint>()
 useEffect(()=>{if(!container.current||candles.length===0)return
  const chart=createChart(container.current,{autoSize:true,height:480,layout:{background:{type:ColorType.Solid,color:'#0b1017'},textColor:'#718096'},grid:{vertLines:{color:'#18212c'},horzLines:{color:'#18212c'}},crosshair:{mode:CrosshairMode.Normal},rightPriceScale:{borderColor:'#263140'},timeScale:{borderColor:'#263140',timeVisible:true,secondsVisible:false},handleScroll:true,handleScale:true})
  const series=chart.addSeries(CandlestickSeries,{upColor:'#22c55e',downColor:'#ef4444',wickUpColor:'#22c55e',wickDownColor:'#ef4444',borderVisible:false,priceLineVisible:false})
  series.setData(candles.map(v=>({time:v.time as UTCTimestamp,open:v.open,high:v.high,low:v.low,close:v.close})))
  let volume:ReturnType<typeof chart.addSeries>|undefined
  if(showVolume&&candles.some(v=>v.volume!==null)){volume=chart.addSeries(HistogramSeries,{priceFormat:{type:'volume'},priceScaleId:'volume',priceLineVisible:false});volume.priceScale().applyOptions({scaleMargins:{top:.82,bottom:0}});volume.setData(candles.filter(v=>v.volume!==null).map(v=>({time:v.time as UTCTimestamp,value:v.volume!,color:v.close>=v.open?'#22c55e55':'#ef444455'})))}
  const overlays:SeriesMarker<UTCTimestamp>[]=[]
  if(showTrades)overlays.push(...markers.map((v,index)=>({...markerStyle[v.action],time:(Date.parse(v.timestamp)/1000) as UTCTimestamp,text:v.action,id:`trade-${index}`})))
  if(showEvents)events.forEach((v,index)=>{const time=at(v.published_at);if(time)overlays.push({time,position:'aboveBar',shape:'square',color:'#60a5fa',text:String(v.event_type??'EVENT'),id:`event-${index}`})})
  if(showPatterns)patterns.forEach((v,index)=>{const time=at(v.detected_at);if(time)overlays.push({time,position:'belowBar',shape:'circle',color:'#a78bfa',text:String(v.pattern_type??'PATTERN'),id:`pattern-${index}`})})
  if(showForecasts)forecasts.forEach((v,index)=>{const time=at(v.created_at);if(time)overlays.push({time,position:'aboveBar',shape:'circle',color:'#facc15',text:`F · ${String(v.direction??'—')}`,id:`forecast-${index}`})})
  if(overlays.length)createSeriesMarkers(series,overlays.sort((a,b)=>Number(a.time)-Number(b.time)))
  if(showTrades){const groups=new Map<string,TradeMarker[]>();markers.forEach(v=>{const key=v.positionId??v.tradeId??'unknown';groups.set(key,[...(groups.get(key)??[]),v])});for(const actions of groups.values()){if(actions.length<2)continue;const connection=chart.addSeries(LineSeries,{color:'#59d4c777',lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});connection.setData(actions.map(v=>({time:(Date.parse(v.timestamp)/1000) as UTCTimestamp,value:v.executionPrice})).sort((a,b)=>Number(a.time)-Number(b.time)))}}
  const focus=focusTimestamp?Date.parse(focusTimestamp)/1000:NaN;const focusIndex=candles.findIndex(v=>v.time>=focus)
  if(Number.isFinite(focus)&&focusIndex>=0)chart.timeScale().setVisibleLogicalRange({from:Math.max(0,focusIndex-30),to:Math.min(candles.length-1,focusIndex+30)});else chart.timeScale().fitContent()
  chart.subscribeCrosshairMove(param=>{const data=param.seriesData.get(series);if(data&&'close' in data)setHover(candles.find(v=>v.time===Number(data.time)))})
  chart.subscribeClick(param=>{if(param.time){const selected=markers.find(v=>Math.floor(Date.parse(v.timestamp)/1000)===Number(param.time));if(selected)onSelectTrade(selected)}})
  return()=>{volume=undefined;chart.remove()}
 },[candles,events,focusTimestamp,forecasts,markers,onSelectTrade,patterns,showEvents,showForecasts,showPatterns,showTrades,showVolume])
 return <div className="chart-wrap"><div ref={container} className="instrument-chart" aria-label="Interactive candlestick chart"/>{hover&&<div className="chart-tooltip"><b>{new Date(hover.time*1000).toLocaleString()}</b><span>O {hover.open} · H {hover.high} · L {hover.low} · C {hover.close}</span>{hover.volume!==null&&<span>Vol {hover.volume.toLocaleString()}</span>}</div>}</div>
}
