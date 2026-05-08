import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useNavigate } from '@tanstack/react-router'
import { getLessonById, getAdjacentLessons, assetUrl, coursePrefix } from '../lessonsStore'
import { QuizView } from './QuizView'

function setCompletedStorage(id: string, value: boolean) {
  try {
    if (value) localStorage.setItem(`completed_${id}`, '1')
    else localStorage.removeItem(`completed_${id}`)
  } catch {}
}

// Strip the original lesson body chrome (header + embedded video player) from
// scraped contentHtml. Keeps only the prose so it doesn't visually duplicate
// what the viewer already renders (title, video panel, navigation).
function cleanContentHtml(raw: string): string {
  if (!raw) return ''
  const doc = new DOMParser().parseFromString(`<div>${raw}</div>`, 'text/html')
  const root = doc.body.firstElementChild as HTMLElement | null
  if (!root) return raw

  // Prefer the TipTap editor content — pure prose, no header chrome.
  const tiptap = root.querySelector('[data-testid="tip-tap-editor-content"]')
  const source = (tiptap as HTMLElement) || root

  // Drop the original lesson header (Lesson N of 197 + duplicated h2 + nav buttons).
  source.querySelectorAll('.flex.items-start.justify-between').forEach(el => el.remove())

  // Drop embedded video player markup — we render video ourselves.
  // IMPORTANT: only remove node-embed wrappers (videos), not node-image.
  // node-image has the same react-renderer/data-node-view-wrapper shape but
  // contains the actual <img> we want to keep.
  source.querySelectorAll(
    [
      'iframe',
      'video',
      'source',
      'media-controller',
      'hls-video',
      'media-theme',
      'template',
      '[class*="react-renderer"][class*="node-embed"]',
      'link[rel="preload"]',
    ].join(','),
  ).forEach(el => el.remove())

  return source.innerHTML
}

export function LessonView() {
  const { lessonId } = useParams({ from: '/$courseId/lesson/$lessonId' })
  const navigate = useNavigate()
  const videoRef = useRef<HTMLVideoElement>(null)
  const [completed, setCompleted] = useState(false)

  const lesson = getLessonById(lessonId)
  const { prev, next } = getAdjacentLessons(lessonId)
  const cleanedHtml = useMemo(
    () => cleanContentHtml(lesson?.contentHtml || ''),
    [lesson?.id, lesson?.contentHtml],
  )

  // Restore saved video position when lesson changes
  useEffect(() => {
    setCompleted(localStorage.getItem(`completed_${lessonId}`) === '1')
    if (!videoRef.current || !lesson?.videoFile) return
    const saved = localStorage.getItem(`pos_${lessonId}`)
    if (saved) {
      videoRef.current.currentTime = parseFloat(saved)
    }
  }, [lessonId, lesson?.videoFile])

  const handleTimeUpdate = () => {
    if (videoRef.current) {
      localStorage.setItem(`pos_${lessonId}`, String(videoRef.current.currentTime))
    }
  }

  const handleComplete = () => {
    // Toggle: if currently complete, uncomplete it (mistake recovery).
    const next = !completed
    setCompletedStorage(lessonId, next)
    setCompleted(next)
  }

  const goNext = () => {
    if (next) navigate({ to: '/$courseId/lesson/$lessonId', params: { courseId: coursePrefix, lessonId: next.id } })
  }

  const goPrev = () => {
    if (prev) navigate({ to: '/$courseId/lesson/$lessonId', params: { courseId: coursePrefix, lessonId: prev.id } })
  }

  if (!lesson) {
    return <div className="p-8 text-gray-500">Lesson not found.</div>
  }

  return (
    <div className="max-w-4xl mx-auto px-6 py-8">
      {lesson.sectionTitle && (
        <p className="text-xs font-semibold text-gray-400 mb-3">{lesson.sectionTitle}</p>
      )}
      {lesson.videoFile ? (
        <div className="rounded-xl overflow-hidden bg-black shadow-2xl">
          <video
            ref={videoRef}
            key={lessonId}
            controls
            crossOrigin="anonymous"
            className="w-full"
            onTimeUpdate={handleTimeUpdate}
            onEnded={handleComplete}
            src={assetUrl(lesson.videoFile)}
            poster={lesson.thumbFile ? assetUrl(lesson.thumbFile) : undefined}
          >
            {(lesson.subtitles || []).map((sub, i) => (
              <track
                key={i}
                kind="subtitles"
                src={assetUrl(sub.file)}
                srcLang={sub.lang || 'en'}
                label={sub.label}
                default={sub.default}
              />
            ))}
          </video>
        </div>
      ) : lesson.videoUrl ? (
        <div className="rounded-xl bg-amber-950/30 border border-amber-900/60 p-6 text-sm">
          <div className="flex items-center gap-2 text-amber-400 font-medium mb-2">
            <svg viewBox="0 0 12 12" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.5">
              <circle cx="6" cy="6" r="5" />
              <path d="M6 3v3M6 8.5v.5" strokeLinecap="round" />
            </svg>
            Video URL captured — download pending
          </div>
          <p className="text-xs text-gray-400 mb-1">Type: <span className="font-mono text-amber-300">{lesson.type}</span></p>
          <p className="text-xs text-gray-400 break-all font-mono mb-3">{lesson.videoUrl}</p>
          <a
            href={lesson.url}
            target="_blank"
            rel="noreferrer"
            className="inline-block text-xs text-blue-400 hover:underline"
          >
            View original lesson page →
          </a>
        </div>
      ) : null}

      {/* Header */}
      <div className="mt-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white">{lesson.title}</h1>
          <a
            href={lesson.url}
            target="_blank"
            rel="noreferrer"
            className="mt-1 inline-flex items-center gap-1.5 text-xs text-blue-400 hover:text-blue-300"
          >
            <svg viewBox="0 0 12 12" className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M5 2H2v8h8V7M7 2h3v3M5 7l5-5" />
            </svg>
            View on Pete Tong DJ Academy
          </a>
        </div>
        <button
          onClick={handleComplete}
          title={completed ? 'Click to mark as not done' : 'Mark this lesson complete'}
          className={`flex-shrink-0 flex items-center gap-2 px-3 py-1.5 rounded-full text-sm border transition-colors group ${
            completed
              ? 'border-green-700 text-green-500 bg-green-950/40 hover:bg-red-950/30 hover:border-red-800 hover:text-red-400'
              : 'border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-300'
          }`}
        >
          <svg viewBox="0 0 10 8" className="w-3 h-2.5 group-hover:hidden" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round">
            <path d="M1 4l3 3 5-5" />
          </svg>
          {completed ? (
            <>
              <svg viewBox="0 0 10 10" className="w-3 h-3 hidden group-hover:block" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round">
                <path d="M1 1l8 8M9 1l-8 8" />
              </svg>
              <span className="group-hover:hidden">Completed</span>
              <span className="hidden group-hover:inline">Undo</span>
            </>
          ) : (
            <span>Mark complete</span>
          )}
        </button>
      </div>

      {/* Quiz: interactive — fetches /<quizFile>.json, lets user answer + grade */}
      {lesson.type === 'quiz' && lesson.quizFile && (
        <div className="mt-8">
          <QuizView quizFile={lesson.quizFile} />
        </div>
      )}

      {/* Pending-content banners — shown when the scraper hasn't extracted this lesson yet */}
      {lesson.type === 'locked' && (
        <div className="mt-8 rounded-xl bg-gray-800/50 border border-gray-700 px-5 py-4 text-sm text-gray-400">
          <p className="font-medium text-gray-300 mb-1">Lesson locked on the platform</p>
          <p>This lesson was still locked when the course was downloaded. Re-run the downloader after completing earlier lessons to unlock it.</p>
          <p className="mt-2 font-mono text-xs text-gray-500">uv run helpers/download_course.py download &lt;course_url&gt; --lesson-ids {lesson.id}</p>
        </div>
      )}
      {lesson.type === 'unknown' && !cleanedHtml && (
        <div className="mt-8 rounded-xl bg-gray-800/50 border border-gray-700 px-5 py-4 text-sm text-gray-400">
          <p className="font-medium text-gray-300 mb-1">Content not yet downloaded</p>
          <p>This lesson wasn't fully scraped. Re-run the downloader to fetch it.</p>
          <p className="mt-2 font-mono text-xs text-gray-500">uv run helpers/download_course.py download &lt;course_url&gt; --lesson-ids {lesson.id}</p>
        </div>
      )}
      {lesson.type === 'quiz' && !lesson.quizFile && (
        <div className="mt-8 rounded-xl bg-amber-950/30 border border-amber-900/60 px-5 py-4 text-sm text-gray-400">
          <p className="font-medium text-amber-300 mb-1">Quiz not yet extracted</p>
          <p>The quiz questions timed out during download. Re-run the downloader to fetch this quiz.</p>
          <p className="mt-2 font-mono text-xs text-gray-500">uv run helpers/download_course.py download &lt;course_url&gt; --lesson-ids {lesson.id}</p>
        </div>
      )}

      {/* Content (cleaned: prose only, no duplicate header or embedded player) */}
      {lesson.type !== 'quiz' && cleanedHtml && (
        <div
          className="mt-8 prose prose-invert prose-sm max-w-none
            prose-headings:text-gray-100 prose-p:text-gray-300
            prose-a:text-blue-400 prose-strong:text-gray-200
            prose-img:rounded-lg prose-img:shadow-lg"
          dangerouslySetInnerHTML={{ __html: cleanedHtml }}
        />
      )}

      {/* File Attachments */}
      {lesson.attachments && lesson.attachments.length > 0 && (
        <div className="mt-8 rounded-xl border border-gray-800 bg-gray-900/50 overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-800 flex items-center gap-2">
            <svg viewBox="0 0 20 16" className="w-4 h-3.5 text-gray-400" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M1 4a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H3a2 2 0 01-2-2V4z" />
            </svg>
            <span className="text-sm font-medium text-gray-300">
              Files ({lesson.attachments.length})
            </span>
          </div>
          <ul className="divide-y divide-gray-800">
            {lesson.attachments.map((att, i) => (
              <li key={i} className="flex items-center justify-between px-4 py-3 hover:bg-gray-800/50 transition-colors">
                <div className="flex items-center gap-3 min-w-0">
                  <svg viewBox="0 0 14 16" className="w-3.5 h-4 text-gray-500 flex-shrink-0" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                    <path d="M8 1H3a2 2 0 00-2 2v10a2 2 0 002 2h8a2 2 0 002-2V6L8 1z" />
                    <path d="M8 1v5h5" />
                  </svg>
                  <span className="text-sm text-gray-200 truncate">{att.name}</span>
                  {att.size && (
                    <span className="text-xs text-gray-600 flex-shrink-0">{att.size}</span>
                  )}
                </div>
                <a
                  href={assetUrl(att.file)}
                  download={att.name}
                  className="ml-4 flex-shrink-0 flex items-center gap-1.5 px-3 py-1 rounded-full text-xs text-blue-400 border border-blue-900/60 hover:bg-blue-950/40 transition-colors"
                >
                  <svg viewBox="0 0 12 12" className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                    <path d="M6 1v7M3 5l3 3 3-3M1 10h10" />
                  </svg>
                  Download
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Navigation */}
      <div className="mt-12 flex justify-between items-center border-t border-gray-800 pt-6">
        <button
          onClick={goPrev}
          disabled={!prev}
          className="flex items-center gap-2 text-sm text-gray-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
        >
          <svg viewBox="0 0 8 14" className="w-2 h-3.5" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round">
            <path d="M7 1L1 7l6 6" />
          </svg>
          {prev ? (
            <span className="max-w-xs truncate">{prev.title}</span>
          ) : (
            <span>Previous</span>
          )}
        </button>

        <span className="text-xs text-gray-600">
          {lesson.lessonIndex + 1}
        </span>

        <button
          onClick={goNext}
          disabled={!next}
          className="flex items-center gap-2 text-sm text-gray-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
        >
          {next ? (
            <span className="max-w-xs truncate">{next.title}</span>
          ) : (
            <span>Next</span>
          )}
          <svg viewBox="0 0 8 14" className="w-2 h-3.5" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round">
            <path d="M1 1l6 6-6 6" />
          </svg>
        </button>
      </div>
    </div>
  )
}
