import Typography from '@mui/material/Typography';
import { useParams } from 'react-router-dom';

export default function TaskDetailPage() {
  const { name } = useParams<{ name: string }>();
  return <Typography variant="h4">Task: {name}</Typography>;
}
