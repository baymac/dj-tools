import { createRootRoute, createRoute, createRouter, redirect } from '@tanstack/react-router'
import { Root } from './components/Root'
import { LessonView } from './components/LessonView'
import { getLessonById, lessons } from './lessonsStore'

export const rootRoute = createRootRoute({
  component: Root,
})

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  beforeLoad: () => {
    if (lessons.length > 0) {
      throw redirect({
        to: '/lesson/$lessonId',
        params: { lessonId: lessons[0].id },
      })
    }
  },
  component: () => (
    <div className="flex items-center justify-center h-full text-gray-500">
      No lessons loaded. Run the scraper first.
    </div>
  ),
})

export const lessonRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/lesson/$lessonId',
  loader: ({ params }) => {
    const lesson = getLessonById(params.lessonId)
    if (!lesson) throw new Error(`Lesson not found: ${params.lessonId}`)
    return lesson
  },
  component: LessonView,
})

const routeTree = rootRoute.addChildren([indexRoute, lessonRoute])

export const router = createRouter({ routeTree })

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}
