import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Topology from './pages/Topology'
import Inference from './pages/Inference'
import Activity from './pages/Activity'

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Topology />} />
        <Route path="/topology" element={<Topology />} />
        <Route path="/inference" element={<Inference />} />
        <Route path="/activity" element={<Activity />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  )
}
