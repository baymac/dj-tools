import type { Lesson, Section } from './types'

export let lessons: Lesson[] = []

export async function loadLessons(): Promise<void> {
  const res = await fetch('/lessons.json')
  if (!res.ok) {
    throw new Error(
      `lessons.json not found (${res.status}). Run the scraper first:\n` +
      `  uv run helpers/download_course.py download <course_url>`
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
