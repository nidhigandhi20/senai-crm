import SentimentChart from '../components/analytics/SentimentChart';
import CategoryChart from '../components/analytics/CategoryChart';
import useAnalytics from '../hooks/useAnalytics';

const AnalyticsDashboard = () => {
  const { trend, categories } = useAnalytics();

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-3xl font-bold">Analytics Dashboard</h1>

      <div className="grid grid-cols-2 gap-6">
        <SentimentChart data={trend} />
        <CategoryChart data={categories} />
      </div>

      <div className="grid grid-cols-3 gap-6">
        <div className="bg-white shadow rounded p-4">
          <h3 className="font-semibold text-lg mb-3">At Risk Accounts</h3>
          <p>No critical accounts.</p>
        </div>

        <div className="bg-white shadow rounded p-4">
          <h3 className="font-semibold text-lg mb-3">Auto Reply Rate</h3>
          <p>78%</p>
        </div>

        <div className="bg-white shadow rounded p-4">
          <h3 className="font-semibold text-lg mb-3">Average Confidence</h3>
          <p>0.89</p>
        </div>
      </div>
    </div>
  );
};

export default AnalyticsDashboard;