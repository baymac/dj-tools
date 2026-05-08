import React from 'react'
import ReactDOM from 'react-dom/client'
import { RouterProvider } from '@tanstack/react-router'
import { router } from './router'
import { loadCourses, loadLessons } from './lessonsStore'
import './index.css'

const STORAGE_KEY = 'selectedCourse'

async function boot() {
  const courses = await loadCourses()

  if (courses.length === 0) {
    document.getElementById('root')!.innerHTML = `
      <div style="font-family:monospace;padding:2rem;color:#ef4444">
        No courses found. Run the scraper first:<br/>
        <span style="color:#9ca3af">uv run helpers/download_course.py download &lt;course_url&gt;</span>
      </div>
    `
    return
  }

  const savedId = localStorage.getItem(STORAGE_KEY)
  const course = courses.find(c => c.id === savedId) || courses[0]

  try {
    await loadLessons(course.id, course.name)
    localStorage.setItem(STORAGE_KEY, course.id)
    ReactDOM.createRoot(document.getElementById('root')!).render(
      <React.StrictMode>
        <RouterProvider router={router} />
      </React.StrictMode>
    )
  } catch (err: any) {
    document.getElementById('root')!.innerHTML = `
      <div style="font-family:monospace;padding:2rem;color:#ef4444">
        <strong>Error loading course:</strong><br/><br/>${err.message}
      </div>
    `
  }
}

boot()
