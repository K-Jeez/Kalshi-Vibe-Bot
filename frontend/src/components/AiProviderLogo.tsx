import React from 'react'
import type { AiProvider } from '../api'
import { aiProviderDisplayName } from '../api'
import geminiLogo from '../assets/ai-providers/gemini.png'
import xaiLogo from '../assets/ai-providers/xai.png'

type Props = {
  provider: AiProvider
  className?: string
}

/** Brand mark for the model that ran an analysis (Gemini or xAI). */
export const AiProviderLogo: React.FC<Props> = ({ provider, className = 'h-10 w-10' }) => {
  const isGemini = provider === 'gemini'
  return (
    <img
      src={isGemini ? geminiLogo : xaiLogo}
      alt=""
      title={aiProviderDisplayName(provider)}
      className={`shrink-0 object-contain ${isGemini ? 'mix-blend-screen' : ''} ${className}`}
    />
  )
}
