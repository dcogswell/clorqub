import * as echarts from 'echarts'
import { useEffect, useRef } from 'react'

export const CHART_COLORS = ['#2dd4bf', '#f59e0b', '#818cf8', '#f472b6', '#a3e635', '#fb923c']

export function baseGrid() {
  return {
    backgroundColor: 'transparent',
    textStyle: { color: '#9ca3af' },
    grid: { left: 48, right: 16, top: 32, bottom: 28 },
    tooltip: {
      trigger: 'axis' as const,
      backgroundColor: '#1e2228',
      borderColor: '#2dd4bf',
      textStyle: { color: '#e5e7eb', fontSize: 12 },
    },
    xAxis: {
      type: 'time' as const,
      axisLine: { lineStyle: { color: '#374151' } },
      axisLabel: { color: '#9ca3af', fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      axisLabel: { color: '#9ca3af', fontSize: 10 },
      splitLine: { lineStyle: { color: '#26292e' } },
    },
    legend: { textStyle: { color: '#9ca3af', fontSize: 11 }, top: 0 },
    color: CHART_COLORS,
  }
}

export default function Chart({
  option,
  height = 260,
  onClick,
}: {
  option: echarts.EChartsOption
  height?: number
  onClick?: (params: unknown) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!ref.current) return
    const chart = echarts.init(ref.current, undefined, { renderer: 'canvas' })
    chartRef.current = chart
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(ref.current)
    return () => {
      ro.disconnect()
      chart.dispose()
    }
  }, [])

  useEffect(() => {
    chartRef.current?.setOption(option, true)
  }, [option])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !onClick) return
    chart.on('click', onClick)
    return () => {
      chart.off('click', onClick)
    }
  }, [onClick])

  return <div ref={ref} style={{ height }} className="w-full" />
}
