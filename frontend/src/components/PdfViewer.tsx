import { useEffect, useMemo, useRef, useState } from 'react'
import * as pdfjsLib from 'pdfjs-dist'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerSrc

export interface PdfHighlight {
  id: string
  /** 待高亮的原文片段，来自后端 ReviewFinding.excerpt */
  excerpt: string
  color: string
  active?: boolean
  pageNo?: number | null
}

/** pdfjs 文本块，字段按 getTextContent() 返回结构收窄。 */
interface PdfTextItem {
  str: string
  transform: number[]
  width: number
  height: number
}

interface Rect {
  left: number
  top: number
  width: number
  height: number
}

interface TextPiece {
  start: number
  end: number
  rect: Rect
}

interface PageModel {
  pageNumber: number
  width: number
  height: number
  pieces: TextPiece[]
  compactText: string
  /** compactText 下标 → pageText 下标，忽略空格差异后再映射回来 */
  compactToStart: number[]
  compactToEnd: number[]
  drawn: boolean
}

const compact = (text: string): string => text.replace(/\s/g, '')

function findPieces(page: PageModel, excerpt: string): TextPiece[] {
  const needle = compact(excerpt)
  if (!needle) return []
  const at = page.compactText.indexOf(needle)
  if (at < 0) return []

  const start = page.compactToStart[at]
  const end = page.compactToEnd[at + needle.length - 1] ?? start + 1
  if (start === undefined) return []
  return page.pieces.filter((piece) => piece.end > start && piece.start < end)
}

export default function PdfViewer({
  fileUrl,
  highlights,
  scale = 1.3,
}: {
  fileUrl: string
  highlights: PdfHighlight[]
  scale?: number
}) {
  const docRef = useRef<PDFDocumentProxy | null>(null)
  const canvasRefs = useRef(new Map<number, HTMLCanvasElement>())
  const [pages, setPages] = useState<PageModel[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // 1) 载入文档，抽出每页文本块坐标（不画布，先建结构）
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setPages([])
    canvasRefs.current.clear()

    pdfjsLib
      .getDocument({ url: fileUrl })
      .promise.then(async (doc) => {
        if (cancelled) return
        docRef.current = doc
        const models: PageModel[] = []

        for (let number = 1; number <= doc.numPages; number += 1) {
          const page = await doc.getPage(number)
          const viewport = page.getViewport({ scale })
          const content = await page.getTextContent()

          const pieces: TextPiece[] = []
          let pageText = ''
          for (const item of content.items as PdfTextItem[]) {
            if (!item?.str) continue
            const x = item.transform[4]
            const y = item.transform[5]
            // 字形高度取 transform 的纵向分量，缺省时退回块高度
            const glyphHeight = Math.abs(item.transform[3]) || item.height || 10
            const topLeft = viewport.convertToViewportPoint(x, y + glyphHeight)
            const bottomRight = viewport.convertToViewportPoint(x + item.width, y)
            const start = pageText.length
            pageText += item.str
            pieces.push({
              start,
              end: pageText.length,
              rect: {
                left: topLeft[0],
                top: Math.min(topLeft[1], bottomRight[1]),
                width: Math.max(1, bottomRight[0] - topLeft[0]),
                height: Math.max(6, Math.abs(bottomRight[1] - topLeft[1])),
              },
            })
          }

          const compactToStart: number[] = []
          const compactToEnd: number[] = []
          for (let index = 0; index < pageText.length; index += 1) {
            if (/\s/.test(pageText[index])) continue
            compactToStart.push(index)
            compactToEnd.push(index + 1)
          }

          models.push({
            pageNumber: number,
            width: viewport.width,
            height: viewport.height,
            pieces,
            compactText: compact(pageText),
            compactToStart,
            compactToEnd,
            drawn: false,
          })
        }

        if (!cancelled) setPages(models)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'PDF 加载失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
      docRef.current = null
    }
  }, [fileUrl, scale])

  // 2) 结构挂好后逐页绘制画布
  useEffect(() => {
    const doc = docRef.current
    if (!doc) return

    pages.forEach((page, index) => {
      if (page.drawn) return
      const canvas = canvasRefs.current.get(page.pageNumber)
      if (!canvas) return

      void doc.getPage(page.pageNumber).then(async (pdfPage) => {
        const viewport = pdfPage.getViewport({ scale })
        canvas.width = viewport.width
        canvas.height = viewport.height
        const context = canvas.getContext('2d')
        if (!context) return
        await pdfPage.render({ canvasContext: context, viewport, canvas }).promise
        setPages((current) =>
          current.map((item, position) =>
            position === index ? { ...item, drawn: true } : item,
          ),
        )
      })
    })
  }, [pages, scale])

  const overlays = useMemo(() => {
    const byPage = new Map<number, { key: string; rect: Rect; color: string; active: boolean }[]>()
    for (const page of pages) {
      const boxes: { key: string; rect: Rect; color: string; active: boolean }[] = []
      for (const highlight of highlights) {
        // 已知页码时只在该页匹配，避免同一句话在其他页被重复高亮
        if (highlight.pageNo && highlight.pageNo !== page.pageNumber) continue
        for (const piece of findPieces(page, highlight.excerpt)) {
          boxes.push({
            key: `${highlight.id}-${piece.start}`,
            rect: piece.rect,
            color: highlight.color,
            active: Boolean(highlight.active),
          })
        }
      }
      if (boxes.length) byPage.set(page.pageNumber, boxes)
    }
    return byPage
  }, [pages, highlights])

  useEffect(() => {
    const active = highlights.find((item) => item.active)
    if (!active) return
    document
      .querySelector(`[data-pdf-page="${active.pageNo ?? 1}"]`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [highlights])

  if (loading) return <div className="p-6 text-sm text-slate-500">正在加载合同原件…</div>

  return (
    <div className="relative">
      {error && (
        <div className="mb-3 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800 ring-1 ring-inset ring-amber-600/20">
          原件渲染失败（{error}）。可切换到"文本视图"核对引用；PDF 无文字层时请先做 OCR。
        </div>
      )}
      {pages.map((page) => (
        <div
          key={page.pageNumber}
          data-pdf-page={page.pageNumber}
          className="relative mb-3 border border-slate-200 bg-white"
          style={{ width: page.width, height: page.height }}
        >
          <canvas
            className="block"
            ref={(node) => {
              if (node) canvasRefs.current.set(page.pageNumber, node)
              else canvasRefs.current.delete(page.pageNumber)
            }}
          />
          <div className="pointer-events-none absolute inset-0">
            {(overlays.get(page.pageNumber) ?? []).map((box) => (
              <div
                key={box.key}
                className="absolute rounded-[2px] transition-all"
                style={{
                  left: box.rect.left,
                  top: box.rect.top,
                  width: box.rect.width,
                  height: box.rect.height,
                  backgroundColor: box.color,
                  outline: box.active ? '2px solid rgba(15, 23, 42, 0.6)' : undefined,
                }}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
