import React from 'react'
import ReactDOM from 'react-dom/client'
import { RouterProvider } from '@tanstack/react-router'
import { router } from './router'
import { loadLessons } from './lessonsStore'
import './index.css'

loadLessons()
  .then(() => {
    ReactDOM.createRoot(document.getElementById('root')!).render(
      <React.StrictMode>
        <RouterProvider router={router} />
      </React.StrictMode>
    )
  })
  .catch(err => {
    document.getElementById('root')!.innerHTML = `
      <div style="font-family:monospace;padding:2rem;color:#ef4444">
        <strong>Error loading lessons:</strong><br/><br/>${err.message}
      </div>
    `
  })
