import { Outlet, useParams } from '@tanstack/react-router'
import { Sidebar } from './Sidebar'
import { getSections } from '../lessonsStore'

export function Root() {
  // Subscribing to params causes Root to re-render on course change,
  // so getSections() picks up the freshly loaded lessons array.
  useParams({ strict: false })
  const sections = getSections()
  return (
    <div className="flex h-screen overflow-hidden bg-gray-950 text-gray-100">
      <Sidebar sections={sections} />
      <main className="flex-1 overflow-y-auto">
        <Outlet />
      </main>
    </div>
  )
}
