import assert from 'node:assert/strict'
import test from 'node:test'
import { availableTimeframes, instrumentFromRow, mapCandles, mapRows, mapTradeMarkers, probabilityLabel, tradeSummary } from './marketData.ts'

test('candle API mapping is chronological, typed and deduplicated',()=>{
 const candles=mapCandles({candles:[{opened_at:'2026-01-01T01:00:00Z',open:2,high:3,low:1,close:2.5,volume:null,source:{provider:'p'}},{opened_at:'2026-01-01T00:00:00Z',open:1,high:2,low:.5,close:1.5,volume:10,source:{provider:'p'}},{opened_at:'2026-01-01T00:00:00Z',open:9,high:9,low:9,close:9}]})
 assert.equal(candles.length,2);assert.deepEqual(candles.map(v=>v.close),[1.5,2.5]);assert.equal(candles[0].provider,'p')
})

test('empty history maps to an explicit empty series and timeframes stay backend-owned',()=>{
 assert.deepEqual(mapCandles({candles:[]}),[]);assert.deepEqual(availableTimeframes({available_timeframes:['1m','1h']}),['1m','1h'])
})

test('raw forecast scores are never presented as probabilities',()=>{
 assert.equal(probabilityLabel({raw_score:.91,probability_up:null,probability_down:null}),'Unavailable — not calibrated')
 assert.equal(probabilityLabel({probability_up:.62}),'62.0% up')
})

test('paper trade marker mapping preserves real execution and costs',()=>{
 const markers=mapTradeMarkers({markers:[{action:'ENTER',timestamp:'2026-01-01T00:00:00Z',execution_price:101,quantity:2,fees:1,slippage:.5,strategy:'NOVA_COMPOSITE',position_id:'p',trade_id:'t',reason:['qualified']},{action:'HOLD',timestamp:'2026-01-01T01:00:00Z',execution_price:102}]})
 assert.equal(markers.length,1);assert.deepEqual(markers[0],{action:'ENTER',timestamp:'2026-01-01T00:00:00Z',executionPrice:101,quantity:2,fees:1,slippage:.5,strategy:'NOVA_COMPOSITE',reason:['qualified'],positionId:'p',tradeId:'t'})
})

test('event and pattern overlays only map persisted rows',()=>{
 assert.deepEqual(mapRows({events:[{event_type:'NEWS'}]},'events'),[{event_type:'NEWS'}]);assert.deepEqual(mapRows({patterns:[{pattern_type:'breakout'}]},'patterns'),[{pattern_type:'breakout'}])
})

test('trade inspector metrics and row navigation use only available fields',()=>{
 assert.deepEqual(tradeSummary({trade:{realized_pnl:4,fees:1,mfe:8,mae:-2}}),{gross:null,fees:1,slippage:null,net:4,mfe:8,mae:-2,capture:null})
 assert.equal(instrumentFromRow({instrument:'BTC-USD'}),'BTC-USD');assert.equal(instrumentFromRow({instruments:['NVDA']}),'NVDA')
})
