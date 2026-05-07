import { useState } from 'react'
import { Link, useParams } from '@tanstack/react-router'
import type { Section, LessonType } from '../types'

interface Props {
  sections: Section[]
}

function isCompleted(id: string): boolean {
  try {
    return localStorage.getItem(`completed_${id}`) === '1'
  } catch {
    return false
  }
}

// We deliberately do NOT show a "locked" indicator. The viewer treats every
// lesson as accessible — the lock state is a scraper-internal concept.
const TYPE_LABEL: Record<LessonType, { label: string; color: string }> = {
  locked:        { label: '',       color: 'text-gray-500' },  // hide — not user-facing
  video_circle:  { label: 'video',  color: 'text-blue-400' },
  video_dyntube: { label: 'video',  color: 'text-blue-400' },
  quiz:          { label: 'quiz',   color: 'text-yellow-400' },
  exercise:      { label: 'task',   color: 'text-purple-400' },
  guide:         { label: 'guide',  color: 'text-emerald-400' },
  content:       { label: 'text',   color: 'text-gray-500' },
  unknown:       { label: '',       color: 'text-gray-500' },
}

function CompletionDot({ id }: { id: string }) {
  const done = isCompleted(id)
  return (
    <span className={`flex-shrink-0 w-4 h-4 rounded-full border flex items-center justify-center mr-2 ${
      done ? 'bg-green-600 border-green-600 text-white' : 'border-gray-600'
    }`}>
      {done && (
        <svg viewBox="0 0 10 8" className="w-2.5 h-2 fill-current">
          <path d="M1 4l3 3 5-5" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round"/>
        </svg>
      )}
    </span>
  )
}

export function Sidebar({ sections }: Props) {
  const params = useParams({ strict: false }) as { lessonId?: string }
  const currentId = params.lessonId

  const [collapsed, setCollapsed] = useState<Record<number, boolean>>({})
  const toggle = (idx: number) =>
    setCollapsed(c => ({ ...c, [idx]: !c[idx] }))

  const totalLessons = sections.reduce((s, sec) => s + sec.lessons.length, 0)
  const completedCount = sections
    .flatMap(s => s.lessons)
    .filter(l => isCompleted(l.id)).length

  return (
    <aside className="w-72 bg-gray-900 border-r border-gray-800 flex flex-col flex-shrink-0 overflow-hidden">
      <div className="p-4 border-b border-gray-800">
        <h1 className="font-semibold text-white text-sm">Pete Tong DJ Academy</h1>
        <p className="text-xs text-gray-500 mt-1">
          {completedCount}/{totalLessons} lessons
        </p>
        <div className="mt-2 h-1 bg-gray-800 rounded-full overflow-hidden">
          <div
            className="h-full bg-blue-600 rounded-full transition-all"
            style={{ width: `${totalLessons ? (completedCount / totalLessons) * 100 : 0}%` }}
          />
        </div>
      </div>

      <nav className="flex-1 overflow-y-auto">
        {sections.map(section => (
          <div key={section.index}>
            <button
              onClick={() => toggle(section.index)}
              className="w-full flex items-center justify-between px-4 py-2.5 text-xs font-semibold text-gray-400 uppercase tracking-wider bg-gray-900 hover:bg-gray-800 transition-colors"
            >
              <span>{section.title}</span>
              <svg
                viewBox="0 0 6 10"
                className={`w-2 h-3 fill-current transition-transform ${
                  collapsed[section.index] ? '' : 'rotate-90'
                }`}
              >
                <path d="M1 1l4 4-4 4" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round"/>
              </svg>
            </button>

            {!collapsed[section.index] && section.lessons.map(lesson => (
              <Link
                key={lesson.id}
                to="/lesson/$lessonId"
                params={{ lessonId: lesson.id }}
                className={`flex items-center px-4 py-2.5 text-sm border-l-2 hover:bg-gray-800/60 transition-colors ${
                  lesson.id === currentId
                    ? 'border-blue-500 bg-gray-800 text-white'
                    : 'border-transparent text-gray-300 hover:text-white'
                }`}
              >
                <CompletionDot id={lesson.id} />
                <span className="truncate">{lesson.title}</span>
                {lesson.type && (
                  <span className={`ml-auto flex-shrink-0 text-[10px] uppercase tracking-wide ${TYPE_LABEL[lesson.type]?.color || 'text-gray-600'}`}>
                    {TYPE_LABEL[lesson.type]?.label || lesson.type}
                  </span>
                )}
              </Link>
            ))}
          </div>
        ))}
      </nav>
    </aside>
  )
}
