import {
  PieChart,
  Pie,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts';

const CategoryChart = ({ data }) => {
  return (
    <div className="bg-white rounded shadow p-4 h-[400px]">
      <h2 className="text-xl font-semibold mb-4">Category Breakdown</h2>

      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={data}
            dataKey="count"
            nameKey="category"
            outerRadius={140}
          >
            {data.map((_, index) => (
              <Cell key={index} />
            ))}
          </Pie>

          <Tooltip />
        </PieChart>
      </ResponsiveContainer>
    </div>
  );
};

export default CategoryChart;