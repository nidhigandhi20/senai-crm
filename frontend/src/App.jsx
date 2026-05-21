import { BrowserRouter, Routes, Route } from 'react-router-dom';
import MissionControlInbox from './views/MissionControlInbox';
import ThreadWorkspace from './views/ThreadWorkspace';
import AnalyticsDashboard from './views/AnalyticsDashboard';
import Sidebar from './components/layout/Sidebar';

const App = () => {
  return (
    <BrowserRouter>
      <div className="flex bg-slate-100 min-h-screen">
        <Sidebar />

        <div className="flex-1">
          <Routes>
            <Route path="/" element={<MissionControlInbox />} />
            <Route path="/thread" element={<ThreadWorkspace />} />
            <Route path="/analytics" element={<AnalyticsDashboard />} />
          </Routes>
        </div>
      </div>
    </BrowserRouter>
  );
};

export default App;