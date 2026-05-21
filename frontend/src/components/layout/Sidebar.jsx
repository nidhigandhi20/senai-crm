import { Link } from 'react-router-dom';

const Sidebar = () => {
  return (
    <div className="w-64 h-screen bg-slate-900 text-white p-5 flex flex-col gap-4">
      <h1 className="text-2xl font-bold">SenAI</h1>

      <Link to="/" className="hover:text-cyan-400">
        Mission Control
      </Link>

      <Link to="/analytics" className="hover:text-cyan-400">
        Analytics
      </Link>
    </div>
  );
};

export default Sidebar;