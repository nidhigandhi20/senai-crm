import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from 'recharts';

const SentimentChart = ({ data }) => {
  return (
    <div className="bg-white rounded shadow p-4 h-[400px]">
      <h2 className="text-xl font-semibold mb-4">
        Sentiment Trend
      </h2>

      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <XAxis dataKey="date" />
          <YAxis />
          <Tooltip />
          <Line
            type="monotone"
            dataKey="sentiment_score"
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
};

export default SentimentChart;