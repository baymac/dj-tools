import type { Lesson, Section } from './types'

export interface CourseEntry {
  id: string
  name: string
  lessonCount: number
}

export let availableCourses: CourseEntry[] = []
export let lessons: Lesson[] = []
export let coursePrefix = ''
export let courseName = ''

export function assetUrl(path: string): string {
  if (!path) return path
  return `/${coursePrefix}/${path}`
}

export async function loadCourses(): Promise<CourseEntry[]> {
  try {
    const res = await fetch('/courses.json')
    if (res.ok) {
      availableCourses = await res.json()
      return availableCourses
    }
  } catch {}
  return []
}

export async function loadLessons(courseId: string, name: string): Promise<void> {
  coursePrefix = courseId
  courseName = name
  const res = await fetch(`/${courseId}/lessons.json`)
  if (!res.ok) {
    throw new Error(
      `lessons.json not found (${res.status}). Run the scraper first:\n` +
      `  uv run helpers/download_course.py download <course_url> --out-dir ~/Music/dj-tools/${courseId}`
    )
  }
  lessons = await res.json()
}

export function getSections(): Section[] {
  const map = new Map<number, Section>()
  for (const lesson of lessons) {
    if (!map.has(lesson.sectionIndex)) {
      map.set(lesson.sectionIndex, {
        index: lesson.sectionIndex,
        title: lesson.sectionTitle,
        lessons: [],
      })
    }
    map.get(lesson.sectionIndex)!.lessons.push(lesson)
  }
  return Array.from(map.values()).sort((a, b) => a.index - b.index)
}

export function getLessonById(id: string): Lesson | undefined {
  return lessons.find(l => l.id === id)
}

export function getAdjacentLessons(id: string): { prev: Lesson | null; next: Lesson | null } {
  const idx = lessons.findIndex(l => l.id === id)
  return {
    prev: idx > 0 ? lessons[idx - 1] : null,
    next: idx < lessons.length - 1 ? lessons[idx + 1] : null,
  }
}
