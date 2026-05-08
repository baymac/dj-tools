import { useEffect, useState } from 'react'
import { assetUrl } from '../lessonsStore'

interface QuizOption {
  id: string
  text: string
  correct: boolean
}

interface QuizQuestion {
  id: string
  text: string
  image?: string
  options: QuizOption[]
}

interface QuizData {
  id: string
  title: string
  url: string
  questions: QuizQuestion[]
}

interface Props {
  quizFile: string  // e.g. "quizzes/2543944.json"
}

export function QuizView({ quizFile }: Props) {
  const [quiz, setQuiz] = useState<QuizData | null>(null)
  const [error, setError] = useState<string>('')
  // Selection per question — selectedByQuestion[qIdx] = optionIdx (single-select)
  const [selectedByQuestion, setSelectedByQuestion] = useState<Record<number, number>>({})
  const [submitted, setSubmitted] = useState(false)

  useEffect(() => {
    setQuiz(null); setError(''); setSelectedByQuestion({}); setSubmitted(false)
    fetch(assetUrl(quizFile))
      .then(r => r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`))
      .then((q: QuizData) => setQuiz(q))
      .catch(e => setError(String(e)))
  }, [quizFile])

  if (error) return <div className="rounded-xl bg-red-950/40 border border-red-900 p-4 text-sm text-red-300">Quiz load failed: {error}</div>
  if (!quiz) return <div className="rounded-xl bg-gray-900 border border-gray-800 p-4 text-sm text-gray-500">Loading quiz…</div>

  if (quiz.questions.length === 0) {
    return (
      <div className="rounded-xl bg-gray-900 border border-gray-800 p-6 text-sm text-gray-400">
        Quiz has no extracted questions yet.
        <a href={quiz.url} target="_blank" rel="noreferrer" className="text-blue-400 hover:underline ml-2">
          Take it on the original site
        </a>
      </div>
    )
  }

  const allAnswered = quiz.questions.every((_, i) => i in selectedByQuestion)
  const score = submitted
    ? quiz.questions.filter((q, i) => q.options[selectedByQuestion[i]]?.correct).length
    : 0

  return (
    <div className="space-y-8">
      {quiz.questions.map((q, qIdx) => {
        const selectedIdx = selectedByQuestion[qIdx]
        const correctOptIdx = q.options.findIndex(o => o.correct)
        return (
          <div key={q.id || qIdx} className="rounded-xl bg-gray-900/60 border border-gray-800 p-6">
            <div className="flex items-baseline gap-2 mb-3">
              <span className="text-xs uppercase tracking-wider text-gray-500">Question {qIdx + 1}</span>
            </div>
            <h3 className="text-lg font-semibold text-white mb-4">{q.text}</h3>
            {q.image && (
              <img src={assetUrl(q.image)} alt="" className="rounded-lg max-w-md mb-4 border border-gray-800" />
            )}
            <ul className="space-y-2">
              {q.options.map((opt, oIdx) => {
                const isSelected = selectedIdx === oIdx
                const isCorrect = opt.correct
                let cls = 'border-gray-700 hover:border-gray-500 hover:bg-gray-800/40'
                if (submitted) {
                  if (isCorrect) cls = 'border-green-700 bg-green-950/30 text-green-300'
                  else if (isSelected) cls = 'border-red-700 bg-red-950/30 text-red-300'
                  else cls = 'border-gray-800 text-gray-500'
                } else if (isSelected) {
                  cls = 'border-blue-600 bg-blue-950/30 text-blue-200'
                }
                return (
                  <li key={oIdx}>
                    <button
                      type="button"
                      disabled={submitted}
                      onClick={() => setSelectedByQuestion(prev => ({ ...prev, [qIdx]: oIdx }))}
                      className={`w-full text-left px-4 py-3 rounded-lg border text-sm transition-colors disabled:cursor-default ${cls}`}
                    >
                      <span className="inline-flex items-center gap-3">
                        <span className={`flex-shrink-0 w-5 h-5 rounded-full border flex items-center justify-center ${
                          isSelected ? 'border-current' : 'border-gray-600'
                        }`}>
                          {isSelected && <span className="w-2.5 h-2.5 rounded-full bg-current" />}
                        </span>
                        <span>{opt.text}</span>
                        {submitted && isCorrect && <span className="ml-auto text-xs">✓ correct</span>}
                        {submitted && !isCorrect && isSelected && <span className="ml-auto text-xs">✗</span>}
                      </span>
                    </button>
                  </li>
                )
              })}
            </ul>
            {submitted && correctOptIdx >= 0 && selectedIdx !== correctOptIdx && (
              <p className="mt-3 text-xs text-gray-400">
                Correct answer: <span className="text-green-400 font-medium">{q.options[correctOptIdx].text}</span>
              </p>
            )}
          </div>
        )
      })}

      {!submitted ? (
        <div className="flex justify-end">
          <button
            type="button"
            disabled={!allAnswered}
            onClick={() => setSubmitted(true)}
            className={`px-5 py-2 rounded-full font-semibold text-sm transition-colors ${
              allAnswered
                ? 'bg-blue-600 hover:bg-blue-500 text-white'
                : 'bg-gray-800 text-gray-500 cursor-not-allowed'
            }`}
          >
            {allAnswered ? 'Submit answers' : 'Answer all questions to submit'}
          </button>
        </div>
      ) : (
        <div className="flex items-center justify-between rounded-xl bg-gray-900 border border-gray-800 p-4">
          <div>
            <div className="text-xs uppercase tracking-wider text-gray-500">Score</div>
            <div className="text-2xl font-bold text-white">
              {score}/{quiz.questions.length}
              <span className="ml-2 text-sm font-normal text-gray-400">
                ({Math.round((score / quiz.questions.length) * 100)}%)
              </span>
            </div>
          </div>
          <button
            type="button"
            onClick={() => { setSelectedByQuestion({}); setSubmitted(false) }}
            className="px-4 py-1.5 rounded-full text-sm border border-gray-700 text-gray-300 hover:border-gray-500"
          >
            Retake
          </button>
        </div>
      )}
    </div>
  )
}
