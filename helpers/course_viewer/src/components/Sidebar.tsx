import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from '@tanstack/react-router'
import type { Lesson, Section, LessonType } from '../types'
import {
  courseName,
  coursePrefix,
  availableCourses,
  lessons,
  loadLessons,
} from '../lessonsStore'

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
  locked:        { label: '',       color: 'text-gray-500' },
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

function LessonRow({ lesson, currentId, sectionTitle }: { lesson: Lesson; currentId?: string; sectionTitle?: string }) {
  return (
    <Link
      key={lesson.id}
      to="/$courseId/lesson/$lessonId"
      params={{ courseId: coursePrefix, lessonId: lesson.id }}
      className={`flex items-start px-4 py-2.5 text-sm border-l-2 hover:bg-gray-800/60 transition-colors ${
        lesson.id === currentId
          ? 'border-blue-500 bg-gray-800 text-white'
          : 'border-transparent text-gray-300 hover:text-white'
      }`}
    >
      <CompletionDot id={lesson.id} />
      <span className="flex-1 min-w-0">
        {sectionTitle && (
          <span className="block text-[10px] text-gray-500 mb-0.5 truncate">{sectionTitle}</span>
        )}
        <span className="block leading-snug">{lesson.title}</span>
      </span>
      {lesson.type && TYPE_LABEL[lesson.type]?.label && (
        <span className={`ml-2 flex-shrink-0 text-[10px] uppercase tracking-wide mt-0.5 ${TYPE_LABEL[lesson.type]?.color || 'text-gray-600'}`}>
          {TYPE_LABEL[lesson.type]?.label}
        </span>
      )}
    </Link>
  )
}

export function Sidebar({ sections }: Props) {
  const params = useParams({ strict: false }) as { lessonId?: string }
  const currentId = params.lessonId
  const navigate = useNavigate()

  const [collapsed, setCollapsed] = useState<Record<number, boolean>>({})
  const [query, setQuery] = useState('')
  const toggle = (idx: number) =>
    setCollapsed(c => ({ ...c, [idx]: !c[idx] }))

  const totalLessons = sections.reduce((s, sec) => s + sec.lessons.length, 0)
  const completedCount = sections
    .flatMap(s => s.lessons)
    .filter(l => isCompleted(l.id)).length

  const searchResults = useMemo(() => {
    if (!query.trim()) return null
    const q = query.toLowerCase()
    return sections.flatMap(sec =>
      sec.lessons
        .filter(l => l.title.toLowerCase().includes(q))
        .map(l => ({ lesson: l, sectionTitle: sec.title }))
    )
  }, [query, sections])

  const handleCourseSwitch = async (courseId: string) => {
    if (courseId === coursePrefix) return
    const course = availableCourses.find(c => c.id === courseId)
    if (!course) return
    await loadLessons(course.id, course.name)
    localStorage.setItem('selectedCourse', course.id)
    navigate({
      to: '/$courseId/lesson/$lessonId',
      params: { courseId: course.id, lessonId: lessons[0].id },
    })
  }

  return (
    <aside className="w-96 bg-gray-900 border-r border-gray-800 flex flex-col flex-shrink-0 overflow-hidden">
      {/* Header */}
      <div className="p-4 border-b border-gray-800">
        {availableCourses.length > 1 ? (
          <select
            value={coursePrefix}
            onChange={e => handleCourseSwitch(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded-md px-2 py-1.5 text-sm font-semibold text-white focus:outline-none focus:border-gray-500 cursor-pointer"
          >
            {availableCourses.map(c => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        ) : (
          <h1 className="font-semibold text-white text-sm leading-snug">{courseName || 'DJ Academy'}</h1>
        )}
        <p className="text-xs text-gray-500 mt-2">
          {completedCount}/{totalLessons} lessons
        </p>
        <div className="mt-2 h-1 bg-gray-800 rounded-full overflow-hidden">
          <div
            className="h-full bg-blue-600 rounded-full transition-all"
            style={{ width: `${totalLessons ? (completedCount / totalLessons) * 100 : 0}%` }}
          />
        </div>
      </div>

      {/* Search */}
      <div className="px-3 py-2.5 border-b border-gray-800">
        <div className="relative">
          <svg viewBox="0 0 16 16" className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500 pointer-events-none" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <circle cx="6.5" cy="6.5" r="4.5" />
            <path d="M10 10l3.5 3.5" />
          </svg>
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search lessons…"
            className="w-full bg-gray-800 border border-gray-700 rounded-md pl-8 pr-7 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-gray-500 focus:bg-gray-800"
          />
          {query && (
            <button
              onClick={() => setQuery('')}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
            >
              <svg viewBox="0 0 10 10" className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M1 1l8 8M9 1l-8 8" />
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* Lesson list */}
      <nav className="flex-1 overflow-y-auto">
        {searchResults ? (
          searchResults.length === 0 ? (
            <p className="px-4 py-6 text-sm text-gray-500 text-center">No lessons match "{query}"</p>
          ) : (
            <>
              <p className="px-4 py-2 text-xs text-gray-500">{searchResults.length} result{searchResults.length !== 1 ? 's' : ''}</p>
              {searchResults.map(({ lesson, sectionTitle }) => (
                <LessonRow key={lesson.id} lesson={lesson} currentId={currentId} sectionTitle={sectionTitle} />
              ))}
            </>
          )
        ) : (
          sections.map(section => (
            <div key={section.index}>
              <button
                onClick={() => toggle(section.index)}
                className="w-full flex items-center justify-between px-4 py-2.5 text-xs font-semibold text-gray-400 bg-gray-900 hover:bg-gray-800 transition-colors"
              >
                <span className="truncate pr-2">{section.title}</span>
                <svg
                  viewBox="0 0 6 10"
                  className={`w-2 h-3 flex-shrink-0 fill-current transition-transform ${
                    collapsed[section.index] ? '' : 'rotate-90'
                  }`}
                >
                  <path d="M1 1l4 4-4 4" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round"/>
                </svg>
              </button>

              {!collapsed[section.index] && section.lessons.map(lesson => (
                <LessonRow key={lesson.id} lesson={lesson} currentId={currentId} />
              ))}
            </div>
          ))
        )}
      </nav>
    </aside>
  )
}
